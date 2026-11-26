"""Relay mode: Git sync straight between two GitLabs, no USB drive.

Runs on a machine that reaches both networks. Per project, one cached
repository serves both directions: it first mirrors GitLab A, fetches B's refs
into ``refs/peer/*`` and applies B's changes to A; then it mirrors B, takes A's
(now updated) refs as the peer and applies them to B. Each pass is the same
three-way merge as the USB sync, against one shared base, so conflicts, deletions
and tags behave identically. LFS objects are copied between the servers directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from airtap_sync import paths
from airtap_sync.config import Config, ConfigError, Side
from airtap_sync.gitlab.client import GitLabClient, GitLabError
from airtap_sync.gitlab.credentials import TokenStore, load_token
from airtap_sync.gitlab.lfs import LfsClient
from airtap_sync.gitlab.projects import Project, current_user
from airtap_sync.gitsync.bundle import DIRECT_PEER_REFSPECS
from airtap_sync.gitsync.cache import RepoCache
from airtap_sync.gitsync.context import GitContext
from airtap_sync.gitsync.engine import GitSyncAborted
from airtap_sync.gitsync.gitcmd import CombinedAuth, GitAuth, GitRunner
from airtap_sync.gitsync.lfsscan import find_lfs_objects
from airtap_sync.gitsync.manifest import ProjectRecord, record_from_project
from airtap_sync.gitsync.projectplan import (
    ProjectAction,
    ProjectDecision,
    ProjectPlan,
    detect_renames,
    plan_projects,
)
from airtap_sync.gitsync.projectsync import ProjectSyncResult, sync_project
from airtap_sync.gitsync.refs import needs_import
from airtap_sync.gitsync.refsync import read_local_refs
from airtap_sync.gitsync.report import GitSyncReport
from airtap_sync.gitsync.resultreport import record_result
from airtap_sync.gitsync.state import GitState
from airtap_sync.progress import ProgressCallback, ProgressEvent, Stage, null_progress

log = logging.getLogger(__name__)


@dataclass
class RelayOptions:
    confirm: bool = False
    dry_run: bool = False
    now: datetime | None = None


@dataclass
class RelayContext:
    """Both GitLabs, one shared repository cache and one shared base."""

    a: GitContext
    b: GitContext
    ids_b: GitState  # B's project ids (ids of A live in the shared state)

    def side(self, name: Side) -> GitContext:
        return self.a if name == "A" else self.b

    def close(self) -> None:
        self.a.cache.close()
        self.a.state.close()
        self.ids_b.close()


def open_relay_context(config: Config, store: TokenStore | None = None) -> RelayContext:
    relay = config.relay
    if relay is None:
        raise ConfigError("config.toml 中没有 [relay] 配置")
    tokens = {}
    for side, url in (("A", relay.url_a), ("B", relay.url_b)):
        token = load_token(url, store)
        if not token:
            raise ConfigError(
                f"尚未保存 {url} 的令牌，请先运行：airtap-sync gitlab login"
            )
        tokens[side] = token
    runner = GitRunner(
        CombinedAuth(
            [GitAuth(relay.url_a, tokens["A"]), GitAuth(relay.url_b, tokens["B"])]
        )
    )
    runner.check_version()
    template = relay.template
    cache = RepoCache(template.cache.dir, template.cache.max_size, runner)
    relay_state = paths.state_dir() / "relay"
    state = GitState(relay_state / "state.sqlite")

    def context(side: Side, url: str) -> GitContext:
        return GitContext(
            config=relay.gitlab_config(side),
            client=GitLabClient(url, tokens[side]),
            runner=runner,
            cache=cache,
            state=state,
            safety=config.safety,
            lfs=LfsClient(url, tokens[side]),
        )

    return RelayContext(
        context("A", relay.url_a),
        context("B", relay.url_b),
        GitState(relay_state / "ids_b.sqlite"),
    )


class Relay:
    def __init__(
        self,
        rc: RelayContext,
        staging: Path,
        options: RelayOptions | None = None,
        progress: ProgressCallback = null_progress,
    ) -> None:
        self.rc = rc
        self.staging = staging / "relay"
        self.options = options or RelayOptions()
        self.progress = progress
        self.report = GitSyncReport("A", relay=True, dry_run=self.options.dry_run)
        now = self.options.now or datetime.now(UTC)
        self.stamp = now.strftime("%Y%m%dT%H%M%SZ")
        self.is_admin: dict[str, bool] = {"A": True, "B": True}
        self.records: dict[str, dict[str, ProjectRecord]] = {}
        self.projects: dict[str, dict[str, Project]] = {}
        self.new_ids: dict[str, dict[int, str]] = {"A": {}, "B": {}}
        self.created: dict[str, str] = {}  # key -> path

    # ----- driver --------------------------------------------------------------

    def run(self) -> GitSyncReport:
        rc, state = self.rc, self.rc.a.state
        for side in ("A", "B"):
            ctx = rc.side(side)  # type: ignore[arg-type]
            self._warn_rights(side, ctx)
        self._read("A", rc.a, rc.a.state)
        self._read("B", rc.b, rc.ids_b)
        recs_a, recs_b = self.records["A"], self.records["B"]
        base_projects = state.base_projects()
        if not base_projects:
            self.report.warnings.append(
                "尚未建立基准：两边都有的工程按首次同步处理（不会产生删除）"
            )
        plan_a = plan_projects(recs_a, recs_b, base_projects)  # A is "local"
        plan_b = plan_projects(recs_b, recs_a, base_projects)  # B is "local"
        self.report.projects_total = len(plan_a.decisions)
        self._check_vanished(plan_a, plan_b)
        self._note_changes(plan_a)

        by_a = {d.key: d for d in plan_a.decisions}
        by_b = {d.key: d for d in plan_b.decisions}
        creations = [
            (d.key, "A", recs_b[d.key]) for d in plan_a.of(ProjectAction.CREATE_LOCAL)
        ] + [(d.key, "B", recs_a[d.key]) for d in plan_b.of(ProjectAction.CREATE_LOCAL)]
        if len(creations) > rc.a.safety.max_new_projects and not self.options.confirm:
            self.report.pending_creates += [rec.path for _, _, rec in creations]
            creations = []
        if self.options.dry_run:
            self._dry_run(by_a, creations)
            return self.report

        for key in sorted(set(by_a) | set(by_b)):
            self.report.projects_checked += 1
            decision = by_a.get(key)
            if decision is not None and decision.action is ProjectAction.SYNC:
                self._sync_pair(key)
            elif decision is not None and decision.action is ProjectAction.SKIP:
                if decision.note and decision.local:
                    self.report.warnings.append(f"{decision.path}：{decision.note}")
        for i, (key, target, source_rec) in enumerate(creations, start=1):
            self.progress(
                ProgressEvent(
                    Stage.IMPORT, source_rec.path, i, len(creations), "projects"
                )
            )
            other: Side = "B" if target == "A" else "A"
            self._pull(target, other, key, source_rec, source_rec.path, create=True)  # type: ignore[arg-type]

        new_base = {
            k: p for k, p in base_projects.items() if k in recs_a or k in recs_b
        }
        new_base.update(plan_a.shared)
        new_base.update(self.created)
        state.set_base_projects(new_base)
        for side, ctx_state in (("A", rc.a.state), ("B", rc.ids_b)):
            ids = {p.id: p.full_path for p in self.projects[side].values()}
            ctx_state.set_local_ids({**ids, **self.new_ids[side]})
        self.progress(ProgressEvent(Stage.DONE, self.report.summary()))
        return self.report

    # ----- reading -------------------------------------------------------------

    def _warn_rights(self, side: str, ctx: GitContext) -> None:
        try:
            user = current_user(ctx.client)
        except GitLabError as e:
            raise GitSyncAborted(f"无法访问网络 {side} 的 GitLab：{e}") from None
        ctx.username = user.username
        self.is_admin[side] = user.is_admin
        if not user.is_admin:
            self.report.warnings.append(
                f"网络 {side} 的账号 {user.username} 不是管理员：只能同步有推送权限的"
                "工程，无法在他人的个人命名空间下建工程"
            )

    def _read(self, side: str, ctx: GitContext, ids: GitState) -> None:
        self.progress(ProgressEvent(Stage.SCAN, f"读取网络 {side} 的工程列表"))
        projects = ctx.discover()
        if not projects and ctx.state.base_projects():
            raise GitSyncAborted(
                f"网络 {side} 没有返回任何工程，可能是地址、令牌或权限有误"
            )
        scanned = ctx.scan(projects)
        renames = detect_renames({p.id: p.full_path for p in projects}, ids.local_ids())
        records: dict[str, ProjectRecord] = {}
        for project in projects:
            result = scanned[project.key]
            refs = ctx.ref_filter.apply(result.refs or {}) if result.ok else None
            if not result.ok:
                self.report.failures.append(
                    f"网络 {side}：{project.full_path}：{result.error}"
                )
            records[project.key] = record_from_project(
                project, refs, renames.get(project.key)
            )
        self.records[side] = records
        self.projects[side] = {p.key: p for p in projects}

    def _check_vanished(self, plan_a: ProjectPlan, plan_b: ProjectPlan) -> None:
        limit = self.rc.a.safety.max_deletions
        for side, plan in (("A", plan_a), ("B", plan_b)):
            gone = plan.of(ProjectAction.GONE_LOCAL)
            if len(gone) > limit:
                raise GitSyncAborted(
                    f"有 {len(gone)} 个以前共有的工程在网络 {side} 消失，"
                    f"超过阈值 {limit}，可能是令牌权限不足或 GitLab 异常，已停止"
                )

    def _note_changes(self, plan_a: ProjectPlan) -> None:
        for d in plan_a.decisions:
            if d.action is ProjectAction.GONE_LOCAL:
                self.report.notes.append(
                    f"{d.path}：网络 A 上已不存在（删除、改名或转移？），不会自动重建"
                )
            elif d.action is ProjectAction.GONE_PEER:
                self.report.notes.append(
                    f"{d.path}：网络 B 上已不存在（删除、改名或转移？），不会自动处理"
                )
            elif d.action is ProjectAction.RENAMED_PEER and d.peer:
                self.report.notes.append(
                    f"网络 B 把 {d.peer.renamed_from} 改名为 {d.path}，"
                    "请在网络 A 改成同名后再同步"
                )
            elif d.action is ProjectAction.RENAMED_LOCAL and d.local:
                self.report.notes.append(
                    f"网络 A 把 {d.local.renamed_from} 改名为 {d.path}，"
                    "请在网络 B 改成同名后再同步"
                )

    # ----- syncing -------------------------------------------------------------

    def _sync_pair(self, key: str) -> None:
        """A project both networks have: apply B to A, then A to B."""
        state = self.rc.a.state
        rec_a, rec_b = self.records["A"][key], self.records["B"][key]
        refs_a, refs_b = rec_a.refs or {}, rec_b.refs or {}
        if refs_a == refs_b:
            state.set_base_refs(key, refs_a)
            return
        self.progress(ProgressEvent(Stage.IMPORT, rec_a.path))
        if needs_import(refs_a, refs_b, state.base_refs(key)):
            result = self._pull("A", "B", key, rec_b, rec_a.path)
            if result and result.refs:
                refs_a = result.refs.local_after
        if needs_import(refs_b, refs_a, state.base_refs(key)):
            self._pull("B", "A", key, replace(rec_a, refs=refs_a), rec_b.path)

    def _pull(
        self,
        target: Side,
        source: Side,
        key: str,
        source_rec: ProjectRecord,
        target_path: str,
        *,
        create: bool = False,
    ) -> ProjectSyncResult | None:
        """Bring ``target``'s GitLab in line with ``source_rec`` (the other one)."""
        tgt, src = self.rc.side(target), self.rc.side(source)
        existing = self.projects[target].get(key)
        if existing is not None and not create:
            if existing.archived:
                self.report.notes.append(
                    f"{target_path}：网络 {target} 的工程已归档，无法接收更新"
                )
                return None
            if not existing.can_push(self.is_admin[target]):
                self.report.warnings.append(
                    f"{target_path}：网络 {target} 的账号没有推送权限，未接收更新"
                )
                return None
        source_url = src.clone_url(source_rec.path)

        def import_peer(repo: Path) -> None:
            tgt.runner.run(
                ["fetch", "--quiet", "--no-tags", source_url, *DIRECT_PEER_REFSPECS],
                cwd=repo,
            )

        def copy_lfs(project_path: str, repo: Path) -> None:
            self._copy_lfs(tgt, src, source_rec, project_path, repo, key)

        result = sync_project(
            tgt,
            source_rec,
            target_path,
            peer_side=source,
            bundle=None,
            create=create,
            trash_stamp=self.stamp,
            confirmed=self.options.confirm,
            before_push=copy_lfs,
            import_peer=import_peer,
        )
        if record_result(self.report, result) and result.created:
            self.created[key] = result.path
            if result.project_id is not None:
                self.new_ids[target][result.project_id] = result.path
        return result

    def _copy_lfs(
        self,
        target: GitContext,
        source: GitContext,
        source_rec: ProjectRecord,
        project_path: str,
        repo: Path,
        key: str,
    ) -> None:
        """Copy the LFS objects the refs about to be pushed need, server to server."""
        if target.lfs is None or source.lfs is None:
            return
        known = [
            *read_local_refs(target.runner, repo).values(),
            *target.state.base_refs(key).values(),
        ]
        objects = find_lfs_objects(
            target.runner, repo, (source_rec.refs or {}).values(), known
        )
        self.staging.mkdir(parents=True, exist_ok=True)
        for obj in target.lfs.missing(project_path, objects):
            tmp = self.staging / obj.oid
            try:
                source.lfs.download(source_rec.path, obj, tmp)
                target.lfs.upload(project_path, obj, tmp)
            finally:
                tmp.unlink(missing_ok=True)

    def _dry_run(
        self,
        by_a: dict[str, ProjectDecision],
        creations: list[tuple[str, str, ProjectRecord]],
    ) -> None:
        self.report.created += [f"{rec.path}（→ 网络 {t}）" for _, t, rec in creations]
        state = self.rc.a.state
        for key, d in by_a.items():
            if d.action is not ProjectAction.SYNC:
                continue
            a = self.records["A"][key].refs or {}
            b = self.records["B"][key].refs or {}
            base = state.base_refs(key)
            if a != b and (needs_import(a, b, base) or needs_import(b, a, base)):
                self.report.updated.append(d.path)
