"""One complete sync run: Git and directories around a single outbox commit.

Both parts import first (reading the peer's outbox), then both export into one
``OutboxWriter``, so the peer always sees a consistent pair of manifests, and
share the transfer quota (Git first, directories with what is left).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from airtap_sync import paths
from airtap_sync.config import Config
from airtap_sync.filesync.engine import FilesOptions, FilesSync
from airtap_sync.filesync.report import FilesReport
from airtap_sync.filesync.report import render_markdown as render_files
from airtap_sync.gitsync.context import GitContext
from airtap_sync.gitsync.engine import GitSync, GitSyncOptions
from airtap_sync.gitsync.report import GitSyncReport, body_lines
from airtap_sync.medium.blobs import drop_unwanted_blobs
from airtap_sync.medium.feedback import RECEIVED, Received, load_feedback
from airtap_sync.medium.outbox import OutboxWriter, read_outbox
from airtap_sync.medium.quota import usb_budget
from airtap_sync.medium.volume import Volume
from airtap_sync.progress import ProgressCallback, null_progress


@dataclass
class SyncOptions:
    git: bool = True
    files: bool = True
    confirm: bool = False
    dry_run: bool = False
    now: datetime | None = None


@dataclass
class SyncResult:
    side: str
    git: GitSyncReport | None = None
    files: FilesReport | None = None
    dry_run: bool = False

    @property
    def needs_attention(self) -> bool:
        return any(r is not None and r.needs_attention for r in (self.git, self.files))

    @property
    def has_pending_confirmation(self) -> bool:
        return any(
            r is not None and r.has_pending_confirmation for r in (self.git, self.files)
        )

    def pending_items(self) -> list[str]:
        items: list[str] = []
        if self.git:
            items += self.git.pending_deletes + self.git.pending_creates
        for folder in self.files.folders if self.files else []:
            items += [f"{folder.name}/{p}" for p in folder.pending_deletes]
        return items

    def summaries(self) -> list[str]:
        out = []
        if self.git:
            out.append(f"Git：{self.git.summary()}")
        if self.files:
            out.append(f"目录：{self.files.summary()}")
        return out


def run_sync(
    config: Config,
    volume: Volume,
    staging: Path,
    state_root: Path,
    options: SyncOptions | None = None,
    git_ctx: GitContext | None = None,
    progress: ProgressCallback = null_progress,
) -> SyncResult:
    """Run the requested parts of a sync for ``config.side`` on ``volume``."""
    options = options or SyncOptions()
    side, peer_side = config.side, config.peer_side
    my_dir, peer_dir = volume.outbox_dir(side), volume.outbox_dir(peer_side)
    peer_commit = read_outbox(peer_dir, peer_side)
    previous = read_outbox(my_dir, side)
    feedback_in = load_feedback(peer_dir, peer_commit)
    feedback_out = Received()

    git = (
        GitSync(
            git_ctx,
            volume,
            side,
            config.transfer,
            staging,
            GitSyncOptions(options.confirm, options.dry_run, options.now),
            progress,
            feedback_out,
        )
        if options.git and git_ctx is not None
        else None
    )
    files = (
        FilesSync(
            config.folders,
            volume,
            side,
            staging,
            state_root,
            config.safety,
            config.transfer.part_size,
            FilesOptions(options.confirm, options.dry_run, options.now),
            progress,
            feedback_out,
        )
        if options.files and config.folders
        else None
    )
    result = SyncResult(side, dry_run=options.dry_run)

    if git is not None:
        git.import_phase(peer_commit, feedback_in)
        result.git = git.report
    if files is not None:
        files.import_phase(peer_commit)
        result.files = files.report
    if options.dry_run:
        if files is not None:
            files.finish()
        return result

    budget = usb_budget(volume.root, config.transfer)
    # A part that did not run keeps the manifests of the previous commit.
    retain = tuple(
        prefix
        for prefix, ran in (("git/", git is not None), ("files/", files is not None))
        if not ran
    )
    with OutboxWriter(my_dir, side, retain=retain) as out:
        if git is not None:
            git.export_phase(out, budget, feedback_in, previous)
        if files is not None:
            files.export_phase(out, budget, feedback_in)
        out.write_manifest(RECEIVED, feedback_out.to_bytes())
        if not retain:  # only a full run knows which blobs are still wanted
            wanted = (git.wanted_blobs if git else set()) | (
                files.wanted_blobs if files else set()
            )
            drop_unwanted_blobs(my_dir, wanted)
    if git is not None:
        git.finish()
    if files is not None:
        files.finish()
    return result


def render_sync_markdown(result: SyncResult, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S UTC")
    title = "同步报告（试运行）" if result.dry_run else "同步报告"
    lines = [f"# {title}", "", f"- 端：{result.side}", f"- 时间：{stamp}"]
    lines += [f"- {s}" for s in result.summaries()]
    lines.append("")
    if result.git is not None:
        lines += ["## Git 同步", ""]
        lines += body_lines(result.git, level="###")
    if result.files is not None:
        lines += render_files(result.files)
    return "\n".join(lines)


def write_sync_report(
    result: SyncResult, directories: list[Path], now: datetime | None = None
) -> list[Path]:
    """Write the combined report into each directory; return the files written."""
    now = now or datetime.now(UTC)
    name = f"{now.strftime('%Y%m%d-%H%M%S')}-{result.side}.md"
    text = render_sync_markdown(result, now)
    written = []
    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(text, encoding="utf-8")
            written.append(directory / name)
        except OSError:
            continue  # a read-only drive must not fail the whole sync
    return written


def default_state_root() -> Path:
    return paths.state_dir()
