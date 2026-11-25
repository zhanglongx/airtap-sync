"""Directory sync for one side: import the peer's changes, export ours.

Per logical folder: scan the local directory, three-way merge with the peer's
manifest against the agreed base, apply the changes safely, then publish the
new manifest and the contents the peer lacks. Contents travel through the
shared content-addressed blob area, split into parts and limited by the quota.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from airtap_sync.config import FolderConfig, SafetyConfig, Side, TransferConfig
from airtap_sync.filesync.applier import (
    ApplyError,
    delete_file,
    install_file,
    purge_old_trash,
    validate_relpath,
)
from airtap_sync.filesync.ignore import IgnoreRules
from airtap_sync.filesync.manifest import FolderManifest, manifest_path
from airtap_sync.filesync.planner import FileAction, FileActionKind, plan_files
from airtap_sync.filesync.report import FilesReport, FolderReport
from airtap_sync.filesync.scanner import FileEntry, ScanResult, scan_folder
from airtap_sync.filesync.state import FolderState
from airtap_sync.medium.blobs import blob_base, drop_unwanted_blobs
from airtap_sync.medium.feedback import RECEIVED, Received, load_feedback
from airtap_sync.medium.outbox import (
    Commit,
    OutboxWriter,
    dump_json_gz,
    load_json_gz,
    read_outbox,
    resolve_relpath,
)
from airtap_sync.medium.parts import PartsError, fetch, store
from airtap_sync.medium.quota import Budget, usb_budget
from airtap_sync.medium.volume import Volume
from airtap_sync.progress import ProgressCallback, ProgressEvent, Stage, null_progress

log = logging.getLogger(__name__)


@dataclass
class FilesOptions:
    confirm: bool = False
    dry_run: bool = False
    now: datetime | None = None


class _Folder:
    """Everything one folder needs between the import and the export phase."""

    def __init__(self, config: FolderConfig, state: FolderState, report: FolderReport):
        self.config = config
        self.state = state
        self.report = report
        self.rules = IgnoreRules(config.ignore)
        self.peer: FolderManifest | None = None
        self.peer_files: dict[str, FileEntry] = {}
        self.scan = ScanResult()
        self.usable = True  # False when the root is missing: skip import and export
        self._by_sha: dict[str, list[str]] | None = None

    def paths_with_hash(self, sha256: str) -> list[str]:
        """Local paths (as scanned) whose content has this hash."""
        if self._by_sha is None:
            self._by_sha = {}
            for path, entry in self.scan.files.items():
                self._by_sha.setdefault(entry.sha256, []).append(path)
        return self._by_sha.get(sha256, [])


class FilesSync:
    def __init__(
        self,
        folders: Mapping[str, FolderConfig],
        volume: Volume,
        side: Side,
        staging: Path,
        state_root: Path,
        safety: SafetyConfig,
        part_size: int,
        options: FilesOptions | None = None,
        progress: ProgressCallback = null_progress,
        feedback_out: Received | None = None,
    ) -> None:
        self.configs = dict(folders)
        self.side: Side = side
        self.peer_side: Side = "B" if side == "A" else "A"
        self.my_dir = volume.outbox_dir(side)
        self.peer_dir = volume.outbox_dir(self.peer_side)
        self.staging = staging / "files"
        self.state_root = state_root
        self.safety = safety
        self.part_size = part_size
        self.options = options or FilesOptions()
        self.progress = progress
        self.feedback_out = feedback_out or Received()
        self.report = FilesReport(dry_run=self.options.dry_run)
        now = self.options.now or datetime.now(UTC)
        self.day = now.date()
        self.wanted_blobs: set[str] = set()
        self._folders: dict[str, _Folder] = {}

    # ----- import --------------------------------------------------------------

    def import_phase(self, peer_commit: Commit | None) -> None:
        for name, config in self.configs.items():
            state = FolderState(self.state_root / "files" / name / "state.sqlite")
            folder = _Folder(config, state, FolderReport(name))
            self._folders[name] = folder
            self.report.folders.append(folder.report)
            self._import_folder(folder, peer_commit)

    def _load_peer(self, name: str, commit: Commit | None) -> FolderManifest | None:
        rel = manifest_path(name)
        if commit is None or rel not in commit.manifests:
            return None
        try:
            manifest = FolderManifest.from_json(
                load_json_gz(resolve_relpath(self.peer_dir, rel))
            )
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("peer manifest %s unreadable: %s", rel, e)
            return None
        ok = manifest.side == self.peer_side and manifest.folder == name
        return manifest if ok else None

    def _import_folder(self, folder: _Folder, peer_commit: Commit | None) -> None:
        cfg, rep = folder.config, folder.report
        root = cfg.path
        if not root.is_dir():
            folder.usable = False
            rep.failures.append(f"目录不存在或不可读，已跳过：{root}")
            return
        self.progress(ProgressEvent(Stage.SCAN, f"扫描 {cfg.name}"))
        folder.peer = self._load_peer(cfg.name, peer_commit)
        if folder.peer is not None:
            folder.rules = folder.rules.union(folder.peer.ignore)
            if set(folder.peer.ignore) != set(cfg.ignore):
                rep.warnings.append("两端的忽略规则不一致，已按两端规则的并集处理")
        folder.scan = scan_folder(root, folder.rules, folder.state)
        rep.scanned = len(folder.scan.files)
        rep.warnings += folder.scan.errors

        if folder.peer is None:
            rep.warnings.append("尚未收到对方这个目录的清单，本轮只导出清单")
            return
        peer_files = self._usable_peer_files(folder)
        folder.peer_files = peer_files
        base = folder.state.base()
        if base and not folder.scan.files and not self.options.confirm:
            folder.usable = False
            rep.failures.append(
                "本地目录现在是空的，但以前同步过内容；为防止误删已中止"
                "（确认目录无误后用 --confirm-deletes 重新运行）"
            )
            return

        plan = plan_files(
            folder.scan.files,
            peer_files,
            base,
            local_side=self.side,
            peer_side=self.peer_side,
        )
        deletes = [a for a in plan.actions if a.kind is FileActionKind.DELETE]
        held: set[str] = set()
        if len(deletes) > self.safety.max_deletions and not self.options.confirm:
            held = {a.path for a in deletes}
            rep.pending_deletes += [a.path for a in deletes]
        if self.options.dry_run:
            self._dry_run(folder, plan.actions, held)
            return
        new_base = dict(plan.new_base)
        for action in plan.actions:
            if action.path in held:
                _restore(new_base, action.path, base)
                continue
            if not self._apply(folder, action):
                _restore(new_base, action.path, base)
        folder.state.set_base(new_base)
        purge_old_trash(cfg.path, self.day)
        # What is on disk now is what we publish.
        folder.scan = scan_folder(root, folder.rules, folder.state)
        folder._by_sha = None
        rep.scanned = len(folder.scan.files)

    def _usable_peer_files(self, folder: _Folder) -> dict[str, FileEntry]:
        """The peer's files, minus ignored ones and names we cannot create."""
        usable = {}
        for path, entry in folder.peer.files.items() if folder.peer else []:
            if folder.rules.ignores(path):
                continue
            try:
                validate_relpath(path)
            except ApplyError as e:
                folder.report.failures.append(f"{path}：{e}")
                continue
            usable[path] = entry
        return usable

    # ----- applying ------------------------------------------------------------

    def _apply(self, folder: _Folder, action: FileAction) -> bool:
        """Apply one action; ``False`` if it must be retried on a later run."""
        rep, root = folder.report, folder.config.path
        try:
            if action.kind is FileActionKind.DELETE:
                assert action.local is not None
                delete_file(root, action.path, expect=action.local, day=self.day)
                rep.deleted.append(action.path)
                return True
            if action.kind is FileActionKind.FETCH:
                assert action.peer is not None
                if not self._install_peer(
                    folder, action.path, action.peer, action.local
                ):
                    return False
                rep.fetched.append(action.path)
                return True
            return self._apply_conflict(folder, action)
        except ApplyError as e:
            rep.failures.append(f"{action.path}：{e}")
            return False

    def _apply_conflict(self, folder: _Folder, action: FileAction) -> bool:
        rep, root = folder.report, folder.config.path
        assert action.local and action.peer and action.conflict_path
        copy = action.conflict_path
        who = "对方" if action.winner == "peer" else "本端"
        note = f"{action.path} → {who}较新，另一份保存为 {copy}"
        if action.winner == "peer":
            # Keep our version next to the winner, then take the peer's.
            if not (root / copy).exists():
                install_file(
                    root,
                    copy,
                    root / action.path,
                    action.local,
                    expect=None,
                    day=self.day,
                )
            ok = self._install_peer(folder, action.path, action.peer, action.local)
        else:
            # Ours stays; the peer's version is kept as the copy.
            ok = (root / copy).exists() or self._install_peer(
                folder, copy, action.peer, None
            )
        if ok:
            rep.conflicts.append(note)
        return ok and action.winner == "peer"

    def _install_peer(
        self,
        folder: _Folder,
        rel: str,
        entry: FileEntry,
        expect: FileEntry | None,
    ) -> bool:
        """Put the peer's version of ``rel`` in place; ``False`` = content missing."""
        root = folder.config.path
        source, is_temp = self._content(folder, rel, entry)
        if source is None:
            return False
        try:
            install_file(root, rel, source, entry, expect=expect, day=self.day)
        finally:
            if is_temp:
                source.unlink(missing_ok=True)
        return True

    def _content(
        self, folder: _Folder, rel: str, entry: FileEntry
    ) -> tuple[Path | None, bool]:
        """A local file holding ``entry``'s content: an identical local file, or
        the blob from the peer's outbox. ``(None, _)`` while the blob is missing."""
        root = folder.config.path
        for other in folder.paths_with_hash(entry.sha256):
            e = folder.scan.files[other]
            if other == rel or e.size != entry.size:
                continue
            candidate = root / other
            try:
                st = candidate.stat()
            except OSError:
                continue
            if (st.st_size, st.st_mtime_ns) == (e.size, e.mtime_ns):
                return candidate, False
        sha = entry.sha256
        self.staging.mkdir(parents=True, exist_ok=True)
        dest = self.staging / f"{sha}.content"
        try:
            result = fetch(
                blob_base(self.peer_dir, sha),
                self.staging / f"{sha}.stage",
                dest,
                sha256=sha,
                size=entry.size,
            )
        except PartsError as e:
            folder.report.failures.append(f"{rel}：内容校验失败：{e}")
            return None, False
        if result.staged:
            self.feedback_out.staged[sha] = sorted(result.staged)
        if not result.complete:
            folder.report.deferred.append(f"{rel}：内容尚未传完")
            return None, False
        self.feedback_out.staged.pop(sha, None)
        return dest, True

    def _dry_run(
        self, folder: _Folder, actions: list[FileAction], held: set[str]
    ) -> None:
        rep = folder.report
        for a in actions:
            if a.path in held:
                continue
            if a.kind is FileActionKind.DELETE:
                rep.deleted.append(a.path)
            elif a.kind is FileActionKind.FETCH:
                rep.fetched.append(a.path)
            else:
                rep.conflicts.append(f"{a.path} → {a.conflict_path}")

    # ----- export --------------------------------------------------------------

    def export_phase(
        self, writer: OutboxWriter, budget: Budget, feedback_in: Received
    ) -> None:
        """Publish each folder's manifest and the file contents the peer lacks."""
        pending: list[tuple[int, str, str, Path, _Folder]] = []
        for name, folder in self._folders.items():
            if not folder.usable:
                # Never publish a manifest we do not trust (missing or emptied
                # folder); the peer keeps seeing the last good one.
                writer.keep_previous(manifest_path(name))
                continue
            manifest = FolderManifest(
                self.side, name, tuple(folder.config.ignore), dict(folder.scan.files)
            )
            writer.write_manifest(manifest_path(name), dump_json_gz(manifest.to_json()))
            if folder.peer is None:
                continue  # handshake round: the peer has not shown its folder yet
            peer_hashes = {e.sha256 for e in folder.peer_files.values()}
            seen: set[str] = set()
            for path, entry in folder.scan.files.items():
                known = folder.peer_files.get(path)
                if (known and known.sha256 == entry.sha256) or (
                    entry.sha256 in peer_hashes
                ):
                    continue
                if entry.sha256 in seen:
                    self.wanted_blobs.add(entry.sha256)
                    continue
                seen.add(entry.sha256)
                self.wanted_blobs.add(entry.sha256)
                pending.append(
                    (entry.size, entry.sha256, path, folder.config.path / path, folder)
                )
        pending.sort(key=lambda item: item[0])
        total = len(pending)
        for i, (size, sha, path, source, folder) in enumerate(pending, start=1):
            if i % 25 == 0 or i == total:
                self.progress(ProgressEvent(Stage.EXPORT, "导出文件内容", i, total))
            if budget.remaining <= 0:
                folder.report.unsent += 1
                continue
            try:
                result = store(
                    source,
                    blob_base(self.my_dir, sha),
                    part_size=self.part_size,
                    budget=budget,
                    skip=feedback_in.staged.get(sha, []),
                )
            except (PartsError, OSError) as e:
                folder.report.warnings.append(
                    f"{path}：导出时文件已变化，留待下一轮（{e}）"
                )
                continue
            folder.report.exported += 1
            folder.report.exported_bytes += size
            if not result.complete:
                folder.report.unsent += 1

    def finish(self) -> None:
        """Clean staged content that is no longer wanted and close the state."""
        for folder in self._folders.values():
            folder.state.close()
        wanted = set()
        for folder in self._folders.values():
            if folder.peer is not None:
                wanted |= {e.sha256 for e in folder.peer.files.values()}
        if self.staging.is_dir():
            for f in self.staging.iterdir():
                if f.name.split(".", 1)[0] not in wanted:
                    f.unlink(missing_ok=True)


def _restore(new_base: dict[str, str], path: str, old_base: Mapping[str, str]) -> None:
    """Undo the planned base change for ``path`` (its action did not happen)."""
    if path in old_base:
        new_base[path] = old_base[path]
    else:
        new_base.pop(path, None)


def run_files_sync(
    folders: Mapping[str, FolderConfig],
    volume: Volume,
    side: Side,
    transfer: TransferConfig,
    staging: Path,
    state_root: Path,
    safety: SafetyConfig,
    options: FilesOptions | None = None,
    progress: ProgressCallback = null_progress,
) -> FilesReport:
    """Run the directory sync alone; ``sync.run_sync`` combines it with Git."""
    options = options or FilesOptions()
    run = FilesSync(
        folders,
        volume,
        side,
        staging,
        state_root,
        safety,
        transfer.part_size,
        options,
        progress,
    )
    peer_commit = read_outbox(run.peer_dir, run.peer_side)
    feedback_in = load_feedback(run.peer_dir, peer_commit)
    run.import_phase(peer_commit)
    if options.dry_run:
        run.finish()
        return run.report
    budget = usb_budget(volume.root, transfer)
    with OutboxWriter(run.my_dir, side, retain=("git/",)) as out:
        run.export_phase(out, budget, feedback_in)
        out.write_manifest(RECEIVED, run.feedback_out.to_bytes())
        drop_unwanted_blobs(run.my_dir, run.wanted_blobs)
    run.finish()
    return run.report
