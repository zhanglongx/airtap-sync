"""One Git sync run on one side: import the peer's changes, export ours.

Order of a run:

1. read the peer's outbox (its project manifest, bundles and feedback);
2. read the local GitLab (projects, then ``git ls-remote`` for every project);
3. three-way merge at project level, then at ref level for projects whose peer
   side changed, applying the changes to the local GitLab;
4. rewrite our outbox: manifest, feedback and bundles for what the peer lacks.

Everything is derived from full state (manifests), never from a log of
operations, so a missed day, a lost drive or an interrupted run is repaired by
the next run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from airtap_sync.config import Side, TransferConfig
from airtap_sync.gitlab.client import GitLabError
from airtap_sync.gitlab.lfs import LfsObject
from airtap_sync.gitlab.projects import Project, current_user
from airtap_sync.gitsync.bundle import MissingPrerequisites
from airtap_sync.gitsync.cache import repo_key
from airtap_sync.gitsync.context import GitContext
from airtap_sync.gitsync.gitcmd import GitError, RefMap
from airtap_sync.gitsync.lfsscan import find_lfs_objects
from airtap_sync.gitsync.manifest import (
    BundleInfo,
    ProjectRecord,
    ProjectsManifest,
    record_from_project,
)
from airtap_sync.gitsync.projectplan import (
    ProjectAction,
    ProjectDecision,
    detect_renames,
    plan_projects,
)
from airtap_sync.gitsync.projectsync import sync_project
from airtap_sync.gitsync.refs import needs_import
from airtap_sync.gitsync.refsync import export_bundle, read_local_refs
from airtap_sync.gitsync.report import ExportedBundle, GitSyncReport
from airtap_sync.gitsync.resultreport import record_result
from airtap_sync.gitsync.transport import GIT_MANIFEST, bundle_base
from airtap_sync.medium.atomicio import sha256_file
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


class GitSyncAborted(Exception):
    """The run was stopped because continuing could be wrong or harmful."""


@dataclass
class GitSyncOptions:
    confirm: bool = False  # confirm deletions / creations above the thresholds
    dry_run: bool = False
    now: datetime | None = None


def run_git_sync(
    ctx: GitContext,
    volume: Volume,
    side: Side,
    transfer: TransferConfig,
    staging: Path,
    options: GitSyncOptions | None = None,
    progress: ProgressCallback = null_progress,
) -> GitSyncReport:
    """Run one Git sync on ``side`` against the drive ``volume``.

    Convenience for running Git alone; ``sync.run_sync`` combines it with the
    directory sync and shares one outbox commit, budget and feedback.
    """
    options = options or GitSyncOptions()
    run = GitSync(ctx, volume, side, transfer, staging, options, progress)
    peer_dir, my_dir = run.peer_dir, run.my_dir
    peer_commit = read_outbox(peer_dir, run.peer_side)
    previous = read_outbox(my_dir, side)
    feedback_in = load_feedback(peer_dir, peer_commit)
    run.import_phase(peer_commit, feedback_in)
    if options.dry_run:
        return run.report
    budget = usb_budget(volume.root, transfer)
    with OutboxWriter(my_dir, side, retain=("files/",)) as out:
        run.export_phase(out, budget, feedback_in, previous)
        out.write_manifest(RECEIVED, run.feedback_out.to_bytes())
        drop_unwanted_blobs(my_dir, run.wanted_blobs)
    run.finish()
    return run.report


class GitSync:
    """One side's Git sync, in two phases around the (shared) outbox rewrite."""

    def __init__(
        self,
        ctx: GitContext,
        volume: Volume,
        side: Side,
        transfer: TransferConfig,
        staging: Path,
        options: GitSyncOptions,
        progress: ProgressCallback,
        feedback_out: Received | None = None,
    ) -> None:
        self.ctx = ctx
        self.volume = volume
        self.side: Side = side
        self.peer_side: Side = "B" if side == "A" else "A"
        self.transfer = transfer
        self.staging = staging / "git"
        self.outgoing = staging / "git-outgoing"
        self.outgoing_lfs = staging / "git-outgoing-lfs"
        self.options = options
        self.progress = progress
        self.max_new_projects = ctx.safety.max_new_projects
        self.is_admin = True  # until the user is looked up; be optimistic
        self.created: set[str] = set()
        self.new_local_ids: dict[int, str] = {}
        self._exported_refs: dict[str, RefMap] = {}
        self.report = GitSyncReport(side, dry_run=options.dry_run)
        self.my_dir = volume.outbox_dir(side)
        self.peer_dir = volume.outbox_dir(self.peer_side)
        now = options.now or datetime.now(UTC)
        self.stamp = now.strftime("%Y%m%dT%H%M%SZ")
        self.feedback_out = feedback_out or Received()  # what we tell the peer
        self.peer: ProjectsManifest | None = None
        self.wanted_blobs: set[str] = set()  # LFS objects the bundles still need
        self.local_records: dict[str, ProjectRecord] = {}
        self.local_projects: dict[str, Project] = {}

    # ----- driver --------------------------------------------------------------

    def import_phase(self, peer_commit: Commit | None, feedback_in: Received) -> None:
        """Read both GitLabs' state and apply the peer's changes locally."""
        state = self.ctx.state
        self.peer = peer = self._load_peer_manifest(peer_commit)
        self._read_local()
        base_projects = state.base_projects()
        if not base_projects and not state.known_keys():
            self.report.warnings.append(
                "尚未建立基准（git snapshot / git baseline）：将按首次同步处理，"
                "不会产生删除，但可能传输大量数据"
            )

        plan = None
        peer_records: dict[str, ProjectRecord] = {}
        if peer is not None:
            peer_records = self._peer_records(peer)
            plan = plan_projects(self.local_records, peer_records, base_projects)
            self.report.projects_total = len(plan.decisions)
            self._check_vanished(plan.of(ProjectAction.GONE_LOCAL))
            for d in plan.decisions:
                if d.action in (
                    ProjectAction.GONE_LOCAL,
                    ProjectAction.GONE_PEER,
                    ProjectAction.RENAMED_LOCAL,
                    ProjectAction.RENAMED_PEER,
                ):
                    self.report.notes.append(f"{d.path}：{d.note}")
                elif d.action is ProjectAction.SKIP and d.note:
                    self.report.warnings.append(f"{d.path}：{d.note}")
        else:
            self.report.warnings.append("尚未收到对方的 Git 数据，本轮只导出")

        if self.options.dry_run:
            self._dry_run(plan, peer)
            return

        if peer is not None and plan is not None:
            self._import(plan.decisions, peer)
            new_base = {
                k: p
                for k, p in base_projects.items()
                if k in self.local_records or k in peer_records
            }
            new_base.update(plan.shared)
            for key in self.created:
                new_base[key] = self.local_records[key].path
            state.set_base_projects(new_base)

    def export_phase(
        self,
        writer: OutboxWriter,
        budget: Budget,
        feedback_in: Received,
        previous: Commit | None,
    ) -> None:
        """Write the project manifest and the bundles the peer lacks."""
        self._export(writer, budget, feedback_in, previous)
        state = self.ctx.state
        ids = {p.id: p.full_path for p in self.local_projects.values()}
        state.set_local_ids({**ids, **self.new_local_ids})

    def finish(self) -> None:
        self._cleanup_staging(self.peer)
        self.progress(ProgressEvent(Stage.DONE, self.report.summary()))

    # ----- reading -------------------------------------------------------------

    def _load_peer_manifest(self, commit: Commit | None) -> ProjectsManifest | None:
        if commit is None or GIT_MANIFEST not in commit.manifests:
            return None
        try:
            raw = load_json_gz(resolve_relpath(self.peer_dir, GIT_MANIFEST))
            manifest = ProjectsManifest.from_json(raw)
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("peer git manifest unreadable: %s", e)
            return None
        return manifest if manifest.side == self.peer_side else None

    def _peer_records(self, peer: ProjectsManifest) -> dict[str, ProjectRecord]:
        flt = self.ctx.ref_filter
        records = {}
        for key, rec in peer.projects.items():
            if not self.ctx.in_scope(rec.path):
                continue
            refs = None if rec.refs is None else flt.apply(rec.refs)
            records[key] = ProjectRecord(**{**rec.to_dict(), "refs": refs})
        return records

    def _read_local(self) -> None:
        ctx = self.ctx
        self.progress(ProgressEvent(Stage.SCAN, "读取本端 GitLab 的工程列表"))
        projects = ctx.discover()
        if not projects and ctx.state.base_projects():
            # An empty listing is only plausible on a brand-new GitLab; once
            # projects have been shared it points at a wrong URL or token.
            raise GitSyncAborted(
                "本端 GitLab 没有返回任何工程，可能是地址、令牌或权限有误"
            )
        self.report.projects_total = len(projects)
        self._warn_if_not_admin()
        done = 0

        def on_done(_r: object) -> None:
            nonlocal done
            done += 1
            if done % 25 == 0 or done == len(projects):
                self.progress(
                    ProgressEvent(Stage.SCAN, "检查分支和 tag", done, len(projects))
                )

        scanned = ctx.scan(projects, on_done)
        previous = ctx.state.local_ids()
        renames = detect_renames({p.id: p.full_path for p in projects}, previous)
        for project in projects:
            result = scanned[project.key]
            refs = ctx.ref_filter.apply(result.refs or {}) if result.ok else None
            if not result.ok:
                self.report.failures.append(f"{project.full_path}：{result.error}")
            self.local_records[project.key] = record_from_project(
                project, refs, renames.get(project.key)
            )
            self.local_projects[project.key] = project

    def _warn_if_not_admin(self) -> None:
        try:
            user = current_user(self.ctx.client)
        except GitLabError:
            return  # listing projects worked; do not fail the run over this
        self.is_admin = user.is_admin
        self.ctx.username = user.username
        if not user.is_admin:
            self.report.warnings.append(
                f"当前账号 {user.username} 不是管理员：只能同步有权限看到、"
                "且有推送权限的工程；无法在他人的个人命名空间下建工程，"
                "通常也不能创建群组"
            )

    def _check_vanished(self, gone: list[ProjectDecision]) -> None:
        limit = self.ctx.max_deletions
        if len(gone) > limit:
            raise GitSyncAborted(
                f"有 {len(gone)} 个以前共有的工程在本端消失，超过阈值 {limit}，"
                "可能是令牌权限不足或 GitLab 异常，已停止"
            )

    # ----- import --------------------------------------------------------------

    def _import(self, decisions: list[ProjectDecision], peer: ProjectsManifest) -> None:
        state = self.ctx.state
        work: list[ProjectDecision] = []
        readonly: list[str] = []
        creations = []
        for d in decisions:
            self.report.projects_checked += 1
            if d.action is ProjectAction.SYNC and d.local and d.peer:
                assert d.local.refs is not None and d.peer.refs is not None
                if d.local.refs == d.peer.refs:
                    state.set_base_refs(d.key, d.local.refs)
                    state.set_peer_refs(d.key, d.peer.refs)
                elif needs_import(d.local.refs, d.peer.refs, state.base_refs(d.key)):
                    if d.local.archived:
                        self.report.notes.append(
                            f"{d.path}：本端工程已归档，无法接收更新"
                        )
                    elif not self.local_projects[d.key].can_push(self.is_admin):
                        readonly.append(d.path)
                    else:
                        work.append(d)
                else:
                    state.set_peer_refs(d.key, d.peer.refs)
            elif d.action is ProjectAction.CREATE_LOCAL:
                creations.append(d)

        if readonly:
            shown = "、".join(readonly[:5]) + ("…" if len(readonly) > 5 else "")
            self.report.warnings.append(
                f"{len(readonly)} 个工程本端无推送权限，未接收对方的更新：{shown}"
            )
        if len(creations) > self.max_new_projects and not self.options.confirm:
            self.report.pending_creates += [d.path for d in creations]
            creations = []
        work += creations

        for i, d in enumerate(work, start=1):
            self.progress(ProgressEvent(Stage.IMPORT, d.path, i, len(work), "projects"))
            self._import_one(d, peer, create=d.action is ProjectAction.CREATE_LOCAL)

    def _import_one(
        self, d: ProjectDecision, peer: ProjectsManifest, create: bool
    ) -> None:
        assert d.peer is not None and d.peer.refs is not None
        info = peer.bundles.get(d.key)
        bundle: Path | None = None
        if info is not None:
            fetched = self._fetch_bundle(d, info)
            if fetched is None:
                return  # not complete yet
            bundle = fetched
        elif create and d.peer.refs:
            self.report.deferred.append(f"{d.path}：等待对方发送数据")
            return

        local_path = d.local.path if d.local else d.path
        uploads: list[tuple[LfsObject, Path]] = []
        if info is not None and info.lfs and self.ctx.lfs is not None:
            prepared = self._prepare_lfs(d, info, local_path, create)
            if prepared is None:
                if bundle is not None:
                    bundle.unlink(missing_ok=True)
                return  # waiting for LFS data, or the server was unreachable
            uploads = prepared

        def upload_lfs(project_path: str, _repo: Path) -> None:
            assert self.ctx.lfs is not None
            for obj, path in uploads:
                self.ctx.lfs.upload(project_path, obj, path)

        try:
            result = sync_project(
                self.ctx,
                d.peer,
                local_path,
                peer_side=self.peer_side,
                bundle=bundle,
                create=create,
                trash_stamp=self.stamp,
                confirmed=self.options.confirm,
                before_push=upload_lfs if uploads else None,
            )
        except MissingPrerequisites as e:
            self.feedback_out.needs_full.append(d.key)
            self.report.deferred.append(f"{d.path}：{e}，已请求对方发送完整数据")
            return
        finally:
            if bundle is not None:
                bundle.unlink(missing_ok=True)
            for _obj, path in uploads:
                path.unlink(missing_ok=True)

        if not record_result(self.report, result):
            return
        if result.created:
            self.created.add(d.key)
            if result.project_id is not None:
                self.new_local_ids[result.project_id] = result.path
        refs = result.refs
        assert refs is not None
        # Keep the local record in step with what the manifest should now say.
        record = self.local_records.get(d.key)
        if record is None and result.created:
            self.local_records[d.key] = ProjectRecord(
                **{
                    **d.peer.to_dict(),
                    "refs": refs.local_after,
                    "renamed_from": None,
                    "project_id": result.project_id or 0,
                }
            )
        elif record is not None:
            self.local_records[d.key] = ProjectRecord(
                **{**record.to_dict(), "refs": refs.local_after}
            )

    def _fetch_bundle(self, d: ProjectDecision, info: BundleInfo) -> Path | None:
        """Bring the peer's bundle for ``d`` to a local file, or ``None``."""
        src = bundle_base(self.peer_dir, d.peer.path if d.peer else d.path, info.sha256)
        return self._fetch_file(
            d.path, src, info.sha256, info.size, f"{info.sha256}.bundle", "bundle"
        )

    def _fetch_file(
        self, path: str, src: Path, sha256: str, size: int, name: str, what: str
    ) -> Path | None:
        """Pull a file (or its parts) from the peer's outbox into staging."""
        stage_base = self.staging / f"{sha256}.stage"
        dest = self.staging / name
        self.staging.mkdir(parents=True, exist_ok=True)
        try:
            result = fetch(src, stage_base, dest, sha256=sha256, size=size)
        except PartsError as e:
            self.report.failures.append(f"{path}：{what} 校验失败：{e}")
            return None
        if result.staged:
            self.feedback_out.staged[sha256] = sorted(result.staged)
        if not result.complete:
            self.report.deferred.append(f"{path}：{what} 数据尚未传完")
            return None
        self.feedback_out.staged.pop(sha256, None)
        return dest

    def _prepare_lfs(
        self, d: ProjectDecision, info: BundleInfo, local_path: str, create: bool
    ) -> list[tuple[LfsObject, Path]] | None:
        """Fetch the LFS objects the local GitLab still needs for this project.

        ``None`` means the project must wait: some object has not arrived yet, or
        the server could not be asked. A new project needs every object.
        """
        lfs = self.ctx.lfs
        assert lfs is not None
        objects = [LfsObject(oid, size) for oid, size in info.lfs]
        try:
            needed = objects if create else lfs.missing(local_path, objects)
        except GitLabError as e:
            self.report.failures.append(f"{d.path}：无法查询 LFS 状态：{e}")
            return None
        fetched: list[tuple[LfsObject, Path]] = []
        for obj in needed:
            path = self._fetch_file(
                d.path,
                blob_base(self.peer_dir, obj.oid),
                obj.oid,
                obj.size,
                f"{obj.oid}.lfs",
                f"LFS 对象 {obj.oid[:12]}",
            )
            if path is None:
                for _o, p in fetched:
                    p.unlink(missing_ok=True)
                return None
            fetched.append((obj, path))
        return fetched

    # ----- export --------------------------------------------------------------

    def _export(
        self,
        out: OutboxWriter,
        budget: Budget,
        feedback_in: Received,
        previous_commit: Commit | None,
    ) -> None:
        peer = self.peer
        ctx = self.ctx
        state = ctx.state
        base_projects = state.base_projects()
        previous = self._previous_manifest(previous_commit)
        wants_full = {k.lower() for k in feedback_in.needs_full}

        candidates: list[tuple[int, str, bool, str]] = []
        pending_new = []
        for key, rec in sorted(self.local_records.items()):
            if rec.refs is None or not rec.refs:
                continue
            peer_rec = peer.projects.get(key) if peer else None
            if peer_rec is not None and peer_rec.refs is not None:
                peer_refs = peer_rec.refs
            else:
                # Without a fresh report assume the peer holds what we last agreed.
                peer_refs = state.peer_refs(key) or state.base_refs(key)
            peer_has = key in base_projects or peer_rec is not None
            if peer_has and all(peer_refs.get(r) == o for r, o in rec.refs.items()):
                continue
            full = (not peer_has) or key in wants_full
            if not peer_has:
                pending_new.append(rec.path)
            known = _known(state.peer_refs(key), state.base_refs(key))
            sig = _signature(rec.refs, None if full else known)
            project = self.local_projects.get(key)
            size = project.repository_size if project else 0
            candidates.append((size, key, full, sig))

        if len(pending_new) > self.max_new_projects and not self.options.confirm:
            self.report.pending_creates += [f"{p}（对方将新建）" for p in pending_new]
            skipped = {p.lower() for p in pending_new}
            candidates = [c for c in candidates if c[1] not in skipped]

        candidates.sort()
        bundles: dict[str, BundleInfo] = {}
        for i, (_size, key, full, sig) in enumerate(candidates, 1):
            rec = self.local_records[key]
            if budget.remaining <= 0:
                kept = self._still_valid(key, rec, sig, previous)
                if kept is not None:
                    bundles[key] = kept  # parts already on the drive stay
                else:
                    self.report.unsent.append(rec.path)
                continue
            self.progress(
                ProgressEvent(Stage.EXPORT, rec.path, i, len(candidates), "projects")
            )
            info = self._export_one(key, rec, full, sig, previous, feedback_in, budget)
            if info is not None:
                bundles[key] = info
        self._drop_stale_bundles(bundles)

        records = {
            k: (
                ProjectRecord(**{**r.to_dict(), "refs": self._refs_for(k, r)})
                if r.refs is not None
                else r
            )
            for k, r in self.local_records.items()
        }
        manifest = ProjectsManifest(self.side, records, bundles)
        out.write_manifest(GIT_MANIFEST, dump_json_gz(manifest.to_json()))
        self.report.budget_spent = budget.spent
        self.report.budget_left = budget.remaining

    def _refs_for(self, key: str, rec: ProjectRecord) -> RefMap:
        return self._exported_refs.get(key, rec.refs or {})

    def _still_valid(
        self,
        key: str,
        rec: ProjectRecord,
        signature: str,
        previous: ProjectsManifest | None,
    ) -> BundleInfo | None:
        """The bundle of the previous run, if it is still the right one."""
        if previous is None or key not in previous.bundles:
            return None
        old = previous.bundles[key]
        local = self.outgoing / f"{repo_key(rec.path)}-{old.sha256}.bundle"
        return old if old.signature == signature and local.is_file() else None

    def _previous_manifest(self, commit: Commit | None) -> ProjectsManifest | None:
        if commit is None or GIT_MANIFEST not in commit.manifests:
            return None
        try:
            raw = load_json_gz(resolve_relpath(self.my_dir, GIT_MANIFEST))
            return ProjectsManifest.from_json(raw)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _export_one(
        self,
        key: str,
        rec: ProjectRecord,
        full: bool,
        signature: str,
        previous: ProjectsManifest | None,
        feedback_in: Received,
        budget: Budget,
    ) -> BundleInfo | None:
        self.outgoing.mkdir(parents=True, exist_ok=True)
        info: BundleInfo | None = None
        src: Path | None = None
        if previous is not None and key in previous.bundles:
            old = previous.bundles[key]
            candidate = self.outgoing / f"{repo_key(rec.path)}-{old.sha256}.bundle"
            if old.signature == signature and candidate.is_file():
                info, src = old, candidate

        if info is None or src is None:
            try:
                info, src = self._build_bundle(key, rec, full)
            except (GitError, OSError) as e:
                self.report.failures.append(f"{rec.path}：生成 bundle 失败：{e}")
                return None
            if info is None or src is None:
                return None  # peer already has everything

        skip = feedback_in.staged.get(info.sha256, [])
        base = bundle_base(self.my_dir, rec.path, info.sha256)
        try:
            result = store(
                src, base, part_size=self.transfer.part_size, budget=budget, skip=skip
            )
        except (PartsError, OSError) as e:
            self.report.failures.append(f"{rec.path}：写入 U 盘失败：{e}")
            return None
        self.report.exported.append(
            ExportedBundle(rec.path, info.size, result.complete, info.full)
        )
        if info.lfs:
            info = self._export_lfs(rec, info, feedback_in, budget)
        return info

    def _export_lfs(
        self,
        rec: ProjectRecord,
        info: BundleInfo,
        feedback_in: Received,
        budget: Budget,
    ) -> BundleInfo:
        """Place the bundle's LFS objects on the drive; drop any that cannot be read."""
        lfs = self.ctx.lfs
        if lfs is None:
            return replace(info, lfs=())
        self.outgoing_lfs.mkdir(parents=True, exist_ok=True)
        usable = []
        for oid, size in info.lfs:
            local = self.outgoing_lfs / oid
            if not local.is_file():
                try:
                    lfs.download(rec.path, LfsObject(oid, size), local)
                except GitLabError as e:
                    # Listing it would make the receiver wait for ever; the
                    # receiver's push is then refused with a clear LFS message.
                    self.report.failures.append(
                        f"{rec.path}：LFS 对象 {oid[:12]} 无法读取：{e}"
                    )
                    continue
            usable.append((oid, size))
            try:
                store(
                    local,
                    blob_base(self.my_dir, oid),
                    part_size=self.transfer.part_size,
                    budget=budget,
                    skip=feedback_in.staged.get(oid, []),
                )
            except (PartsError, OSError) as e:
                self.report.failures.append(f"{rec.path}：LFS 对象写入 U 盘失败：{e}")
        return replace(info, lfs=tuple(usable))

    def _build_bundle(
        self,
        key: str,
        rec: ProjectRecord,
        full: bool,
    ) -> tuple[BundleInfo | None, Path | None]:
        ctx = self.ctx
        url = ctx.clone_url(rec.path)
        project = self.local_projects.get(key)
        size = project.repository_size if project else 0
        state = ctx.state
        with ctx.cache.open(rec.path, url, estimated_size=size) as repo:
            refs = ctx.ref_filter.apply(read_local_refs(ctx.runner, repo))
            self._exported_refs = {**self._exported_refs, key: refs}
            peer_refs = state.peer_refs(key)
            tmp = self.outgoing / f"{repo_key(rec.path)}.building"
            made = export_bundle(
                ctx.runner,
                repo,
                tmp,
                refs,
                peer_refs=peer_refs,
                base=state.base_refs(key),
                full=full,
            )
            lfs_objects: list[LfsObject] = []
            if made and ctx.lfs is not None:
                known = (
                    []
                    if full
                    else [*peer_refs.values(), *state.base_refs(key).values()]
                )
                lfs_objects = find_lfs_objects(ctx.runner, repo, refs.values(), known)
        if not made:
            return None, None
        digest = sha256_file(tmp)
        final = self.outgoing / f"{repo_key(rec.path)}-{digest}.bundle"
        shutil.move(str(tmp), final)
        # The signature describes the refs actually bundled.
        known = None if full else _known(peer_refs, state.base_refs(key))
        signature = _signature(refs, known)
        info = BundleInfo(
            digest,
            final.stat().st_size,
            full,
            signature,
            lfs=tuple((o.oid, o.size) for o in lfs_objects),
        )
        return info, final

    def _drop_stale_bundles(self, current: Mapping[str, BundleInfo]) -> None:
        """Remove bundle files on the drive and locally that are no longer wanted."""
        wanted_dirs = {
            repo_key(self.local_records[k].path): info.sha256
            for k, info in current.items()
        }
        root = self.my_dir / "git" / "bundles"
        if root.is_dir():
            for entry in root.iterdir():
                sha = wanted_dirs.get(entry.name)
                if sha is None:
                    shutil.rmtree(entry, ignore_errors=True)
                    continue
                for f in entry.iterdir():
                    if not f.name.startswith(f"{sha}.bundle"):
                        f.unlink(missing_ok=True)
        if self.outgoing.is_dir():
            keep = {f"{k}-{sha}.bundle" for k, sha in wanted_dirs.items()}
            for f in self.outgoing.iterdir():
                if f.name not in keep:
                    f.unlink(missing_ok=True)
        self.wanted_blobs = {oid for info in current.values() for oid, _ in info.lfs}
        if self.outgoing_lfs.is_dir():
            for f in self.outgoing_lfs.iterdir():
                if f.name not in self.wanted_blobs:
                    f.unlink(missing_ok=True)

    # ----- housekeeping -------------------------------------------------------------

    def _cleanup_staging(self, peer: ProjectsManifest | None) -> None:
        if not self.staging.is_dir():
            return
        wanted: set[str] = set()
        if peer:
            for b in peer.bundles.values():
                wanted.add(b.sha256)
                wanted.update(oid for oid, _ in b.lfs)
        for f in self.staging.iterdir():
            sha = f.name.split(".", 1)[0]
            if sha not in wanted:
                f.unlink(missing_ok=True)

    def _dry_run(self, plan, peer: ProjectsManifest | None) -> None:
        """Report what would happen, without touching GitLab or the drive."""
        if plan is None:
            return
        state = self.ctx.state
        for d in plan.decisions:
            self.report.projects_checked += 1
            if d.action is ProjectAction.CREATE_LOCAL:
                self.report.created.append(d.path)
            elif d.action is ProjectAction.SYNC and d.local and d.peer:
                assert d.local.refs is not None and d.peer.refs is not None
                if d.local.refs != d.peer.refs and needs_import(
                    d.local.refs, d.peer.refs, state.base_refs(d.key)
                ):
                    self.report.updated.append(d.path)


def _known(peer_refs: Mapping[str, str], base_refs: Mapping[str, str]) -> set[str]:
    return set(peer_refs.values()) | set(base_refs.values())


def _signature(refs: Mapping[str, str], known: set[str] | None) -> str:
    """Identify a bundle's inputs: the refs it carries and what it assumes."""
    doc = {
        "refs": sorted(refs.items()),
        "known": None if known is None else sorted(known),
    }
    return hashlib.sha256(json.dumps(doc).encode("utf-8")).hexdigest()
