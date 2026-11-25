from __future__ import annotations

import random

import pytest

from airtap_sync.gitsync.refs import (
    ActionKind,
    RefFilter,
    RefPlan,
    conflict_branch,
    plan_refs,
    stale_conflict_branches,
)

H = "refs/heads/"
T = "refs/tags/"


class Dag:
    """Fake commit graph: ``add(name, parent)``; names double as object ids."""

    def __init__(self) -> None:
        self.parents: dict[str, str | None] = {}

    def add(self, name: str, parent: str | None = None) -> str:
        self.parents[name] = parent
        return name

    def is_ancestor(self, a: str, b: str) -> bool:
        node: str | None = b
        while node is not None:
            if node == a:
                return True
            node = self.parents.get(node)
        return False

    def plan(
        self, local: dict, peer: dict, base: dict, *, missing: frozenset = frozenset()
    ) -> RefPlan:
        return plan_refs(
            local,
            peer,
            base,
            is_ancestor=self.is_ancestor,
            has_object=lambda oid: oid not in missing,
        )


@pytest.fixture
def dag() -> Dag:
    d = Dag()
    d.add("c1")
    d.add("c2", "c1")
    d.add("c3", "c2")
    d.add("x2", "c1")  # diverges from c2
    return d


def kinds(plan: RefPlan) -> list[tuple[str, str]]:
    return [(a.kind.value, a.ref) for a in plan.actions]


def test_in_sync_updates_base(dag: Dag) -> None:
    plan = dag.plan({H + "m": "c2"}, {H + "m": "c2"}, {H + "m": "c1"})
    assert plan.actions == []
    assert plan.new_base == {H + "m": "c2"}


def test_both_absent_everywhere_is_noop(dag: Dag) -> None:
    assert dag.plan({}, {}, {}).actions == []


def test_peer_created_branch(dag: Dag) -> None:
    plan = dag.plan({}, {H + "new": "c2"}, {})
    (action,) = plan.actions
    assert (action.kind, action.old, action.new) == (ActionKind.UPDATE, None, "c2")
    assert plan.new_base == {H + "new": "c2"}


def test_peer_fast_forwards(dag: Dag) -> None:
    plan = dag.plan({H + "m": "c1"}, {H + "m": "c3"}, {H + "m": "c1"})
    (action,) = plan.actions
    assert (action.kind, action.old, action.new) == (ActionKind.UPDATE, "c1", "c3")
    assert plan.new_base == {H + "m": "c3"}


def test_only_we_changed_is_left_for_peer(dag: Dag) -> None:
    plan = dag.plan({H + "m": "c3"}, {H + "m": "c1"}, {H + "m": "c1"})
    assert plan.actions == []
    assert plan.new_base == {H + "m": "c1"}  # not agreed yet


def test_we_created_branch_peer_lacks_it(dag: Dag) -> None:
    plan = dag.plan({H + "n": "c1"}, {}, {})
    assert plan.actions == []
    assert H + "n" not in plan.new_base


def test_peer_deleted_unchanged_branch(dag: Dag) -> None:
    plan = dag.plan({H + "m": "c1"}, {}, {H + "m": "c1"})
    (action,) = plan.actions
    assert (action.kind, action.old) == (ActionKind.DELETE, "c1")
    assert plan.new_base == {}


def test_we_deleted_unchanged_branch_peer_still_has_it(dag: Dag) -> None:
    plan = dag.plan({}, {H + "m": "c1"}, {H + "m": "c1"})
    assert plan.actions == []
    assert plan.new_base == {H + "m": "c1"}  # peer will delete it


def test_peer_deleted_but_we_modified_keeps_ours(dag: Dag) -> None:
    plan = dag.plan({H + "m": "c3"}, {}, {H + "m": "c1"})
    assert plan.actions == []
    assert plan.new_base == {H + "m": "c1"}


def test_we_deleted_but_peer_modified_recreates(dag: Dag) -> None:
    plan = dag.plan({}, {H + "m": "c3"}, {H + "m": "c1"})
    (action,) = plan.actions
    assert (action.kind, action.old, action.new) == (ActionKind.UPDATE, None, "c3")


def test_both_changed_peer_ahead_fast_forwards(dag: Dag) -> None:
    plan = dag.plan({H + "m": "c2"}, {H + "m": "c3"}, {H + "m": "c1"})
    assert kinds(plan) == [("update", H + "m")]


def test_both_changed_peer_behind_is_noop(dag: Dag) -> None:
    plan = dag.plan({H + "m": "c3"}, {H + "m": "c2"}, {H + "m": "c1"})
    assert plan.actions == []


def test_diverged_branch_is_a_conflict_and_base_is_kept(dag: Dag) -> None:
    plan = dag.plan({H + "m": "c2"}, {H + "m": "x2"}, {H + "m": "c1"})
    (action,) = plan.actions
    assert action.kind is ActionKind.CONFLICT
    assert (action.old, action.new) == ("c2", "x2")
    assert plan.new_base == {H + "m": "c1"}


def test_both_created_same_name_diverged(dag: Dag) -> None:
    plan = dag.plan({H + "n": "c2"}, {H + "n": "x2"}, {})
    assert kinds(plan) == [("conflict", H + "n")]


def test_peer_rewrote_history_is_a_conflict(dag: Dag) -> None:
    # local == base == c3; peer reset the branch back to c1 (not a fast-forward)
    plan = dag.plan({H + "m": "c3"}, {H + "m": "c1"}, {H + "m": "c3"})
    assert kinds(plan) == [("conflict", H + "m")]


def test_missing_peer_objects_defer(dag: Dag) -> None:
    plan = dag.plan({H + "m": "c1"}, {H + "m": "c3"}, {H + "m": "c1"}, missing={"c3"})
    assert kinds(plan) == [("defer", H + "m")]
    assert plan.new_base == {H + "m": "c1"}
    plan = dag.plan({}, {H + "n": "c3"}, {}, missing={"c3"})
    assert kinds(plan) == [("defer", H + "n")]


def test_new_tag_is_created(dag: Dag) -> None:
    plan = dag.plan({}, {T + "v1": "c1"}, {})
    assert kinds(plan) == [("update", T + "v1")]


def test_tag_deletion_propagates(dag: Dag) -> None:
    plan = dag.plan({T + "v1": "c1"}, {}, {T + "v1": "c1"})
    assert kinds(plan) == [("delete", T + "v1")]


def test_tag_pointing_elsewhere_is_never_overwritten(dag: Dag) -> None:
    both_new = dag.plan({T + "v1": "c1"}, {T + "v1": "c2"}, {})
    assert kinds(both_new) == [("tag-conflict", T + "v1")]
    moved = dag.plan({T + "v1": "c1"}, {T + "v1": "c2"}, {T + "v1": "c1"})
    assert kinds(moved) == [("tag-conflict", T + "v1")]
    assert moved.new_base == {T + "v1": "c1"}


def test_conflict_branch_name() -> None:
    assert conflict_branch("B", H + "feature/x") == H + "airtap/conflict/B/feature/x"


def test_ref_filter() -> None:
    f = RefFilter(
        include_branches=("release/*", "main"), exclude_branches=("release/old",)
    )
    assert f.accepts(H + "main")
    assert f.accepts(H + "release/1.0")
    assert not f.accepts(H + "release/old")
    assert not f.accepts(H + "feature")
    assert f.accepts(T + "anything")
    assert not f.accepts("refs/merge-requests/1/head")
    assert not RefFilter().accepts(H + "airtap/conflict/B/main")
    assert RefFilter().apply({H + "a": "1", H + "airtap/x": "2"}) == {H + "a": "1"}


def test_stale_conflict_branches(dag: Dag) -> None:
    local = {
        H + "m": "c3",  # has absorbed c2
        H + "airtap/conflict/B/m": "c2",  # merged -> stale
        H + "airtap/conflict/B/other": "x2",  # original branch missing -> keep
        H + "airtap/conflict/A/m": "c2",  # belongs to another peer -> ignore
    }
    stale = stale_conflict_branches(local, "B", is_ancestor=dag.is_ancestor)
    assert [(a.kind, a.ref, a.old) for a in stale] == [
        (ActionKind.DELETE, H + "airtap/conflict/B/m", "c2")
    ]
    unmerged = {H + "m": "c2", H + "airtap/conflict/B/m": "x2"}
    assert stale_conflict_branches(unmerged, "B", is_ancestor=dag.is_ancestor) == []


# ----- two-sided simulation ---------------------------------------------------


class Side:
    def __init__(self, dag: Dag) -> None:
        self.dag = dag
        self.refs: dict[str, str] = {}
        self.base: dict[str, str] = {}

    def import_from(self, other: Side) -> None:
        plan = self.dag.plan(self.refs, dict(other.refs), self.base)
        for a in plan.actions:
            if a.kind is ActionKind.UPDATE:
                assert a.new is not None
                self.refs[a.ref] = a.new
            elif a.kind is ActionKind.DELETE:
                del self.refs[a.ref]
        self.base = plan.new_base


def stabilise(a: Side, b: Side, rounds: int = 6) -> None:
    for _ in range(rounds):
        a.import_from(b)
        b.import_from(a)


@pytest.mark.parametrize("seed", range(40))
def test_owned_branches_always_converge(seed: int) -> None:
    """If each branch is written by one side only (the intended workflow),
    both sides converge to identical refs, whatever the order of events."""
    rng = random.Random(seed)
    dag = Dag()
    sides = {"A": Side(dag), "B": Side(dag)}
    owner = {f"{H}{side}-{n}": side for side in "AB" for n in range(4)}
    owner.update({f"{T}{side}-{n}": side for side in "AB" for n in range(2)})
    counter = 0
    last: dict[str, str] = {}  # last tip ever seen per ref; keeps history related

    for _ in range(25):
        ref = rng.choice(sorted(owner))
        me = sides[owner[ref]]
        if ref in me.refs and rng.random() < 0.3:
            del me.refs[ref]
        elif ref.startswith(T):
            # Tags are immutable: (re)create at the same object, never move.
            if ref not in me.refs:
                if ref not in last:
                    counter += 1
                    last[ref] = dag.add(f"n{counter}", None)
                me.refs[ref] = last[ref]
        else:
            counter += 1
            me.refs[ref] = last[ref] = dag.add(f"n{counter}", last.get(ref))
        if rng.random() < 0.5:
            sides["A"].import_from(sides["B"])
        if rng.random() < 0.5:
            sides["B"].import_from(sides["A"])

    stabilise(sides["A"], sides["B"])
    assert sides["A"].refs == sides["B"].refs
    assert sides["A"].base == sides["A"].refs
    assert sides["B"].base == sides["B"].refs
