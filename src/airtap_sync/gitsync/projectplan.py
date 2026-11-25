"""Project-level three-way merge: which projects exist on which side.

Projects are matched by lowercased full path (ids differ between the two
GitLabs after a backup/restore). A project is created on the side that lacks
it only if it was never in the base; a project that *disappeared* from one side
(deleted, renamed or transferred) is reported and never touched automatically.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from airtap_sync.gitsync.manifest import ProjectRecord


class ProjectAction(StrEnum):
    SYNC = "sync"  # exists on both sides: synchronise refs
    CREATE_LOCAL = "create-local"  # peer has it, we lack it and never had it
    GONE_LOCAL = "gone-local"  # was shared; missing here now (report only)
    GONE_PEER = "gone-peer"  # was shared; missing on the peer now (report only)
    RENAMED_PEER = "renamed-peer"  # peer renamed it; we still have the old name
    RENAMED_LOCAL = "renamed-local"  # we renamed it; the peer has the old name
    LOCAL_NEW = "local-new"  # we have it, the peer will create it
    SKIP = "skip"  # refs unknown on a side this round


@dataclass(frozen=True)
class ProjectDecision:
    action: ProjectAction
    key: str
    path: str
    local: ProjectRecord | None = None
    peer: ProjectRecord | None = None
    note: str = ""


@dataclass
class ProjectPlan:
    decisions: list[ProjectDecision] = field(default_factory=list)
    # Keys present on both sides now (before creations); the next project base.
    shared: dict[str, str] = field(default_factory=dict)

    def of(self, action: ProjectAction) -> list[ProjectDecision]:
        return [d for d in self.decisions if d.action is action]


def detect_renames(
    current: Mapping[int, str], previous: Mapping[int, str]
) -> dict[str, str]:
    """``{new_key: old_path}`` for projects whose id is unchanged but path moved."""
    renamed = {}
    for project_id, path in current.items():
        old = previous.get(project_id)
        if old is not None and old.lower() != path.lower():
            renamed[path.lower()] = old
    return renamed


def plan_projects(
    local: Mapping[str, ProjectRecord],
    peer: Mapping[str, ProjectRecord],
    base: Collection[str],
) -> ProjectPlan:
    """Decide what to do for every project key seen on either side."""
    plan = ProjectPlan()
    for key in sorted(set(local) | set(peer)):
        mine, theirs = local.get(key), peer.get(key)
        if mine and theirs:
            plan.shared[key] = mine.path
            if mine.refs is None or theirs.refs is None:
                plan.decisions.append(
                    ProjectDecision(
                        ProjectAction.SKIP,
                        key,
                        mine.path,
                        mine,
                        theirs,
                        "无法读取分支列表",
                    )
                )
            else:
                plan.decisions.append(
                    ProjectDecision(ProjectAction.SYNC, key, mine.path, mine, theirs)
                )
        elif theirs:
            plan.decisions.append(_only_peer(key, theirs, local, base))
        else:
            assert mine is not None
            plan.decisions.append(_only_local(key, mine, peer, base))
    return plan


def _only_peer(
    key: str,
    theirs: ProjectRecord,
    local: Mapping[str, ProjectRecord],
    base: Collection[str],
) -> ProjectDecision:
    if key in base:
        return ProjectDecision(
            ProjectAction.GONE_LOCAL,
            key,
            theirs.path,
            peer=theirs,
            note="本端已不存在（可能被删除、改名或转移），不会自动重建",
        )
    if theirs.renamed_from and theirs.renamed_from.lower() in local:
        old = local[theirs.renamed_from.lower()]
        return ProjectDecision(
            ProjectAction.RENAMED_PEER,
            key,
            theirs.path,
            local=old,
            peer=theirs,
            note=f"对方把 {theirs.renamed_from} 改名为 {theirs.path}，"
            "请在本端改成同名后再同步",
        )
    return ProjectDecision(ProjectAction.CREATE_LOCAL, key, theirs.path, peer=theirs)


def _only_local(
    key: str,
    mine: ProjectRecord,
    peer: Mapping[str, ProjectRecord],
    base: Collection[str],
) -> ProjectDecision:
    if key in base:
        return ProjectDecision(
            ProjectAction.GONE_PEER,
            key,
            mine.path,
            local=mine,
            note="对方已不存在（可能被删除、改名或转移），不会自动处理",
        )
    if mine.renamed_from and mine.renamed_from.lower() in peer:
        return ProjectDecision(
            ProjectAction.RENAMED_LOCAL,
            key,
            mine.path,
            local=mine,
            peer=peer[mine.renamed_from.lower()],
            note=f"本端把 {mine.renamed_from} 改名为 {mine.path}，"
            "请在对方改成同名后再同步",
        )
    return ProjectDecision(
        ProjectAction.LOCAL_NEW,
        key,
        mine.path,
        local=mine,
        note="本端新建，等待对方创建",
    )
