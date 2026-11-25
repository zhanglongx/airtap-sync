"""Ref-level synchronisation of one project: plan, apply, push, export.

The cached bare repository mirrors the local GitLab under ``refs/local/*`` and
holds the peer's objects (imported from bundles) under ``refs/peer/*``. All
functions here take real ref names (``refs/heads/x``) and translate to the
cache namespaces internally.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from airtap_sync.gitsync.bundle import LOCAL_PREFIX, create_bundle
from airtap_sync.gitsync.gitcmd import GitRunner, RefMap
from airtap_sync.gitsync.pusher import push_refs
from airtap_sync.gitsync.refs import (
    HEADS,
    ActionKind,
    RefAction,
    RefFilter,
    conflict_branch,
    plan_refs,
    stale_conflict_branches,
)

log = logging.getLogger(__name__)

TRASH_PREFIX = "refs/airtap-trash"


def to_cache_ref(ref: str) -> str:
    """``refs/heads/x`` -> ``refs/local/heads/x``."""
    return LOCAL_PREFIX + ref.removeprefix("refs/")


def from_cache_ref(ref: str) -> str:
    return "refs/" + ref.removeprefix(LOCAL_PREFIX)


def read_local_refs(runner: GitRunner, repo: Path) -> RefMap:
    """The local GitLab's branches and tags, as mirrored into the cache."""
    raw = runner.list_refs(repo, LOCAL_PREFIX.rstrip("/"))
    return {from_cache_ref(name): oid for name, oid in raw.items()}


@dataclass
class RefSyncResult:
    applied: list[RefAction] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    conflicts: list[RefAction] = field(default_factory=list)
    deferred: list[RefAction] = field(default_factory=list)
    parked: list[str] = field(default_factory=list)
    cleaned: list[str] = field(default_factory=list)
    new_base: RefMap = field(default_factory=dict)
    local_after: RefMap = field(default_factory=dict)  # filtered, for the manifest

    @property
    def deletions(self) -> list[RefAction]:
        return [a for a in self.applied if a.kind is ActionKind.DELETE]


def sync_refs(
    runner: GitRunner,
    repo: Path,
    url: str,
    peer_refs: RefMap,
    base: RefMap,
    *,
    peer_side: str,
    ref_filter: RefFilter,
    ci_skip: bool = True,
    trash_stamp: str = "unknown",
) -> RefSyncResult:
    """Bring the local GitLab's refs in line with the peer's, without forcing.

    ``repo`` must already hold the peer's objects (see ``import_bundle``).
    """
    local_all = read_local_refs(runner, repo)
    local = ref_filter.apply(local_all)
    peer = ref_filter.apply(peer_refs)
    base = ref_filter.apply(base)

    present = runner.existing_objects(repo, peer.values())
    plan = plan_refs(
        local,
        peer,
        base,
        is_ancestor=lambda a, b: runner.is_ancestor(repo, a, b),
        has_object=present.__contains__,
    )
    stale = stale_conflict_branches(
        {r: o for r, o in local_all.items() if r.startswith(HEADS)},
        peer_side,
        is_ancestor=lambda a, b: runner.is_ancestor(repo, a, b),
    )

    result = RefSyncResult(new_base=dict(plan.new_base))
    updates: dict[str, str] = {}
    deletes: dict[str, str] = {}
    forced: dict[str, str] = {}
    for action in plan.actions:
        if action.kind is ActionKind.UPDATE and action.new:
            updates[action.ref] = action.new
        elif action.kind is ActionKind.DELETE and action.old:
            deletes[action.ref] = action.old
        elif action.kind is ActionKind.CONFLICT and action.new:
            result.conflicts.append(action)
            parking = conflict_branch(peer_side, action.ref)
            if local_all.get(parking) != action.new:
                forced[parking] = action.new
        elif action.kind is ActionKind.TAG_CONFLICT:
            result.conflicts.append(action)
        elif action.kind is ActionKind.DEFER:
            result.deferred.append(action)
    for action in stale:
        if action.old:
            deletes[action.ref] = action.old

    # Keep a recoverable copy of everything about to be deleted.
    trash = {
        f"{TRASH_PREFIX}/{trash_stamp}/{ref.removeprefix('refs/')}": old
        for ref, old in deletes.items()
    }
    runner.update_refs(repo, trash)

    pushed = push_refs(
        runner,
        repo,
        url,
        updates=updates,
        forced=forced,
        deletes=deletes,
        ci_skip=ci_skip,
    )
    result.failed = dict(pushed.failed)

    cache_updates: dict[str, str | None] = {}
    for action in plan.actions:
        if action.kind not in (ActionKind.UPDATE, ActionKind.DELETE):
            continue
        if action.ref in pushed.ok:
            result.applied.append(action)
            cache_updates[to_cache_ref(action.ref)] = action.new
        else:  # not applied: the agreement did not happen after all
            _restore_base(result.new_base, action.ref, base)
    for parking in forced:
        if parking in pushed.ok:
            result.parked.append(parking)
            cache_updates[to_cache_ref(parking)] = forced[parking]
    for action in stale:
        if action.ref in pushed.ok:
            result.cleaned.append(action.ref)
            cache_updates[to_cache_ref(action.ref)] = None
    runner.update_refs(repo, cache_updates)

    result.local_after = ref_filter.apply(read_local_refs(runner, repo))
    return result


def _restore_base(new_base: RefMap, ref: str, old_base: RefMap) -> None:
    if ref in old_base:
        new_base[ref] = old_base[ref]
    else:
        new_base.pop(ref, None)


def export_bundle(
    runner: GitRunner,
    repo: Path,
    out: Path,
    local_refs: RefMap,
    *,
    peer_refs: RefMap,
    base: RefMap,
    full: bool = False,
) -> bool:
    """Bundle the objects behind ``local_refs`` that the peer is missing.

    The peer is assumed to hold what it last reported (``peer_refs``) and what
    both sides last agreed on (``base``). With ``full`` nothing is assumed.
    Returns ``False`` if there is nothing to send.
    """
    known = [] if full else [*peer_refs.values(), *base.values()]
    return create_bundle(
        runner,
        repo,
        out,
        [to_cache_ref(r) for r in local_refs],
        known,
    )
