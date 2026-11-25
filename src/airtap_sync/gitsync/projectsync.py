"""Synchronise one project: create it if needed, import the bundle, merge refs."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from airtap_sync.gitlab.client import GitLabError
from airtap_sync.gitlab.projects import (
    archive_project,
    create_project,
    set_default_branch,
)
from airtap_sync.gitsync.bundle import import_bundle
from airtap_sync.gitsync.context import GitContext
from airtap_sync.gitsync.gitcmd import GitError, RefMap
from airtap_sync.gitsync.manifest import ProjectRecord
from airtap_sync.gitsync.refs import HEADS
from airtap_sync.gitsync.refsync import RefSyncResult, sync_refs

log = logging.getLogger(__name__)


@dataclass
class ProjectSyncResult:
    key: str
    path: str
    created: bool = False
    refs: RefSyncResult | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def local_refs(self) -> RefMap | None:
        """The local refs after syncing, for the manifest (``None`` on error)."""
        return self.refs.local_after if self.refs else None


def sync_project(
    ctx: GitContext,
    peer: ProjectRecord,
    local_path: str,
    *,
    peer_side: str,
    bundle: Path | None,
    create: bool,
    trash_stamp: str,
) -> ProjectSyncResult:
    """Bring one project in line with the peer's record.

    With ``create`` the project is first created on the local GitLab (an empty
    repository), then filled by the same ref merge as any other project, and
    finally its default branch and archived state are copied. Problems are
    recorded in the result instead of raised, so other projects carry on.
    """
    key = peer.key
    result = ProjectSyncResult(key=key, path=local_path, created=create)
    if peer.refs is None:
        result.error = "对方未能读取该工程的分支列表"
        return result
    try:
        if create:
            created = create_project(
                ctx.client,
                full_path=peer.path,
                name=peer.name,
                description=peer.description,
                visibility=peer.visibility,
                namespace_kind=peer.namespace_kind,
                namespace_path=peer.namespace_path,
                lfs_enabled=peer.lfs_enabled,
            )
            local_path = created.full_path
            result.path = local_path
            ctx.state.add_base_project(key, local_path)
            project_id = created.id
        url = ctx.clone_url(local_path)
        base = {} if create else ctx.state.base_refs(key)
        with ctx.cache.open(local_path, url) as repo:
            if bundle is not None:
                import_bundle(ctx.runner, repo, bundle)
            result.refs = sync_refs(
                ctx.runner,
                repo,
                url,
                peer.refs,
                base,
                peer_side=peer_side,
                ref_filter=ctx.ref_filter,
                ci_skip=ctx.config.ci_skip,
                trash_stamp=trash_stamp,
            )
        ctx.state.set_base_refs(key, result.refs.new_base)
        ctx.state.set_peer_refs(key, peer.refs)
        if create:
            _finish_creation(ctx, peer, project_id, result)
    except (GitError, GitLabError) as e:
        log.warning("project %s failed: %s", local_path, e)
        result.error = str(e)
    return result


def _finish_creation(
    ctx: GitContext, peer: ProjectRecord, project_id: int, result: ProjectSyncResult
) -> None:
    """Copy the default branch and the archived state to a freshly created project."""
    assert result.refs is not None
    if peer.default_branch and HEADS + peer.default_branch in result.refs.local_after:
        try:
            set_default_branch(ctx.client, project_id, peer.default_branch)
        except GitLabError as e:
            result.notes.append(f"无法设置默认分支 {peer.default_branch}：{e}")
    elif peer.default_branch:
        result.notes.append(f"默认分支 {peer.default_branch} 尚未同步，未设置")
    if peer.archived:
        try:
            archive_project(ctx.client, project_id)
        except GitLabError as e:
            result.notes.append(f"无法归档：{e}")
