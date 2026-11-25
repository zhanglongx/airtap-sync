"""Three-way merge of branches and tags between the local and the peer GitLab.

This module is pure: it decides what to do from three ref maps and two
callbacks, and touches neither git nor the network.

For every ref name the inputs are ``mine`` (local GitLab), ``theirs`` (peer, from
its last manifest) and ``base`` (base, the last state both sides were seen agreeing
on). Branches are only ever fast-forwarded. Anything that would need a forced
update becomes a conflict that is reported and, for branches, parked under
``airtap/conflict/<peer>/<branch>`` for a human to merge.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from airtap_sync.gitsync.gitcmd import RefMap

HEADS = "refs/heads/"
TAGS = "refs/tags/"
CONFLICT_PREFIX = "airtap/conflict/"
# Branches under this prefix are owned by airtap-sync and never synchronised.
RESERVED_BRANCH_PREFIX = "airtap/"


class ActionKind(StrEnum):
    UPDATE = "update"  # create, or fast-forward, to ``new``
    DELETE = "delete"  # remove; ``old`` is the expected current value
    CONFLICT = "conflict"  # diverged branch: park the peer's version
    TAG_CONFLICT = "tag-conflict"  # same tag name, different objects
    DEFER = "defer"  # peer's objects are not here yet; try again later


@dataclass(frozen=True)
class RefAction:
    kind: ActionKind
    ref: str
    old: str | None = None
    new: str | None = None
    note: str = ""


@dataclass
class RefPlan:
    actions: list[RefAction] = field(default_factory=list)
    # Base after every UPDATE/DELETE succeeds; conflicts and deferrals keep
    # their old base value, so they are re-evaluated on the next run.
    new_base: RefMap = field(default_factory=dict)

    def of_kind(self, kind: ActionKind) -> list[RefAction]:
        return [a for a in self.actions if a.kind is kind]


def conflict_branch(peer_side: str, ref: str) -> str:
    """Name of the parking branch for a diverged ``refs/heads/<name>``."""
    return f"{HEADS}{CONFLICT_PREFIX}{peer_side}/{ref.removeprefix(HEADS)}"


@dataclass(frozen=True)
class RefFilter:
    """Which refs take part in the sync (branch include/exclude globs)."""

    include_branches: tuple[str, ...] = ()
    exclude_branches: tuple[str, ...] = ()

    def accepts(self, ref: str) -> bool:
        if ref.startswith(TAGS):
            return True
        if not ref.startswith(HEADS):
            return False
        name = ref.removeprefix(HEADS)
        if name.startswith(RESERVED_BRANCH_PREFIX):
            return False
        if self.include_branches and not any(
            fnmatch.fnmatchcase(name, pattern) for pattern in self.include_branches
        ):
            return False
        return not any(
            fnmatch.fnmatchcase(name, pattern) for pattern in self.exclude_branches
        )

    def apply(self, refs: RefMap) -> RefMap:
        return {r: o for r, o in refs.items() if self.accepts(r)}


def plan_refs(
    local: RefMap,
    peer: RefMap,
    base: RefMap,
    *,
    is_ancestor: Callable[[str, str], bool],
    has_object: Callable[[str], bool],
) -> RefPlan:
    """Decide how to bring ``local`` in line with ``peer``.

    ``is_ancestor(a, b)`` tells whether commit ``a`` is reachable from ``b``;
    ``has_object(oid)`` whether the object is present in the local repository.
    """
    plan = RefPlan(new_base=dict(base))
    for ref in sorted(set(local) | set(peer) | set(base)):
        mine, theirs, base_oid = local.get(ref), peer.get(ref), base.get(ref)
        action = _decide(ref, mine, theirs, base_oid, is_ancestor, has_object)
        if action is not None:
            plan.actions.append(action)
        _update_base(plan.new_base, ref, mine, theirs, action)
    return plan


def _decide(
    ref: str,
    mine: str | None,
    theirs: str | None,
    base_oid: str | None,
    is_ancestor: Callable[[str, str], bool],
    has_object: Callable[[str], bool],
) -> RefAction | None:
    if mine == theirs:
        return None
    is_tag = ref.startswith(TAGS)

    if mine == base_oid:  # only the peer changed
        if theirs is None:
            return RefAction(ActionKind.DELETE, ref, old=mine)
        return _take_peer(ref, mine, theirs, is_tag, is_ancestor, has_object)

    if theirs == base_oid:  # only we changed; the peer will import it
        return None

    # Both sides changed, differently.
    if mine is None:  # we deleted, the peer modified: modification wins
        assert theirs is not None
        return _take_peer(ref, None, theirs, is_tag, is_ancestor, has_object)
    if theirs is None:  # the peer deleted, we modified: keep ours
        return None
    if is_tag:
        return RefAction(
            ActionKind.TAG_CONFLICT,
            ref,
            old=mine,
            new=theirs,
            note="两端的同名 tag 指向不同对象",
        )
    if not has_object(theirs):
        return RefAction(
            ActionKind.DEFER, ref, old=mine, new=theirs, note="对方的提交尚未送达"
        )
    if is_ancestor(mine, theirs):
        return RefAction(ActionKind.UPDATE, ref, old=mine, new=theirs)
    if is_ancestor(theirs, mine):
        return None  # the peer is merely behind
    return RefAction(
        ActionKind.CONFLICT, ref, old=mine, new=theirs, note="两端的分支已分叉"
    )


def _take_peer(
    ref: str,
    mine: str | None,
    theirs: str,
    is_tag: bool,
    is_ancestor: Callable[[str, str], bool],
    has_object: Callable[[str], bool],
) -> RefAction:
    if not has_object(theirs):
        return RefAction(
            ActionKind.DEFER, ref, old=mine, new=theirs, note="对方的提交尚未送达"
        )
    if mine is None:
        return RefAction(ActionKind.UPDATE, ref, old=None, new=theirs)
    if is_tag:
        return RefAction(
            ActionKind.TAG_CONFLICT,
            ref,
            old=mine,
            new=theirs,
            note="对方改动了已有的 tag",
        )
    if is_ancestor(mine, theirs):
        return RefAction(ActionKind.UPDATE, ref, old=mine, new=theirs)
    return RefAction(
        ActionKind.CONFLICT, ref, old=mine, new=theirs, note="对方改写了历史（非快进）"
    )


def _update_base(
    base: RefMap,
    ref: str,
    mine: str | None,
    theirs: str | None,
    action: RefAction | None,
) -> None:
    """Record agreement in ``base`` once local and peer are known to match."""
    if action is None:
        agreed = mine == theirs
    elif action.kind in (ActionKind.UPDATE, ActionKind.DELETE):
        agreed = True  # local becomes ``p`` once the action is applied
    else:
        agreed = False
    if not agreed:
        return
    if theirs is None:
        base.pop(ref, None)
    else:
        base[ref] = theirs


def stale_conflict_branches(
    local: RefMap,
    peer_side: str,
    *,
    is_ancestor: Callable[[str, str], bool],
    reserved: Iterable[str] = (),
) -> list[RefAction]:
    """Conflict branches whose content has since been merged into the original.

    ``local`` must be the *unfiltered* local branch map, since conflict
    branches live under the reserved ``airtap/`` prefix.
    """
    prefix = f"{HEADS}{CONFLICT_PREFIX}{peer_side}/"
    stale = []
    for ref, oid in sorted(local.items()):
        if not ref.startswith(prefix) or ref in set(reserved):
            continue
        original = HEADS + ref.removeprefix(prefix)
        current = local.get(original)
        if current is not None and is_ancestor(oid, current):
            stale.append(
                RefAction(
                    ActionKind.DELETE, ref, old=oid, note="分叉已合并，清理冲突分支"
                )
            )
    return stale
