from __future__ import annotations

from datetime import UTC, datetime

import pytest

from airtap_sync.filesync.planner import (
    FileActionKind,
    conflict_name,
    pick_winner,
    plan_files,
)
from airtap_sync.filesync.scanner import FileEntry

T0 = int(datetime(2026, 11, 20, tzinfo=UTC).timestamp() * 1e9)
DAY = 86_400 * 10**9


def entry(sha: str, day: int = 0) -> FileEntry:
    return FileEntry(size=len(sha), mtime_ns=T0 + day * DAY, sha256=sha)


def plan(local: dict, peer: dict, base: dict):
    return plan_files(local, peer, base, local_side="A", peer_side="B")


def kinds(p) -> dict[str, FileActionKind]:
    return {a.path: a.kind for a in p.actions}


def test_identical_files_agree() -> None:
    p = plan({"a": entry("x")}, {"a": entry("x")}, {"a": "old"})
    assert p.actions == []
    assert p.new_base == {"a": "x"}


def test_both_absent_is_dropped_from_base() -> None:
    assert plan({}, {}, {"a": "x"}).new_base == {}


def test_peer_created_file() -> None:
    p = plan({}, {"a": entry("x")}, {})
    assert kinds(p) == {"a": FileActionKind.FETCH}
    assert p.new_base == {"a": "x"}


def test_peer_modified_file() -> None:
    p = plan({"a": entry("1")}, {"a": entry("2")}, {"a": "1"})
    assert kinds(p) == {"a": FileActionKind.FETCH}
    assert p.new_base == {"a": "2"}


def test_peer_deleted_file() -> None:
    p = plan({"a": entry("1")}, {}, {"a": "1"})
    assert kinds(p) == {"a": FileActionKind.DELETE}
    assert p.new_base == {}


def test_only_we_changed_leaves_it_to_the_peer() -> None:
    p = plan({"a": entry("2")}, {"a": entry("1")}, {"a": "1"})
    assert p.actions == []
    assert p.new_base == {"a": "1"}  # not agreed yet


def test_we_created_file_peer_lacks_it() -> None:
    p = plan({"a": entry("x")}, {}, {})
    assert p.actions == [] and p.new_base == {}


def test_we_deleted_unchanged_file() -> None:
    p = plan({}, {"a": entry("1")}, {"a": "1"})
    assert p.actions == []
    assert p.new_base == {"a": "1"}  # the peer will delete it


def test_deleted_here_but_modified_there_modification_wins() -> None:
    p = plan({}, {"a": entry("2")}, {"a": "1"})
    assert kinds(p) == {"a": FileActionKind.FETCH}
    assert p.new_base == {"a": "2"}


def test_deleted_there_but_modified_here_keeps_ours() -> None:
    p = plan({"a": entry("2")}, {}, {"a": "1"})
    assert p.actions == []
    assert p.new_base == {"a": "1"}


def test_both_modified_newer_peer_wins_and_local_becomes_conflict_copy() -> None:
    p = plan(
        {"d/a.txt": entry("mine", 1)}, {"d/a.txt": entry("theirs", 2)}, {"d/a.txt": "0"}
    )
    (action,) = p.actions
    assert action.kind is FileActionKind.CONFLICT
    assert action.winner == "peer"
    assert action.conflict_path and action.conflict_path.endswith(".txt")
    assert "-A-20261121-" in action.conflict_path  # the loser is ours (side A)
    assert p.new_base == {"d/a.txt": "theirs"}


def test_both_modified_newer_local_wins() -> None:
    p = plan({"a": entry("mine", 3)}, {"a": entry("theirs", 2)}, {"a": "0"})
    (action,) = p.actions
    assert action.winner == "local"
    assert "-B-20261122-" in (action.conflict_path or "")  # the loser is the peer's
    assert p.new_base == {"a": "0"}  # the peer has yet to take our version


def test_both_created_differently_is_a_conflict() -> None:
    p = plan({"a": entry("1", 1)}, {"a": entry("2", 2)}, {})
    assert kinds(p) == {"a": FileActionKind.CONFLICT}


def test_conflict_name_is_the_same_on_both_sides() -> None:
    loser = entry("deadbeef" * 8, 1)
    # Side A computes it as "peer loses" and side B as "local loses".
    a_view = conflict_name("x/report.tar.gz", "B", loser)
    b_view = conflict_name("x/report.tar.gz", "B", loser)
    assert a_view == b_view == "x/report.tar.sync-conflict-B-20261121-deadbeef.gz"
    assert (
        conflict_name(".bashrc", "A", loser)
        == ".bashrc.sync-conflict-A-20261121-deadbeef"
    )
    assert conflict_name("plain", "A", loser).startswith("plain.sync-conflict-A-")


@pytest.mark.parametrize(
    ("mine", "theirs", "winner"),
    [
        (entry("a", 2), entry("b", 1), "local"),
        (entry("a", 1), entry("b", 2), "peer"),
        (entry("b", 1), entry("a", 1), "local"),  # equal times: larger hash wins
        (entry("a", 1), entry("b", 1), "peer"),
    ],
)
def test_pick_winner(mine: FileEntry, theirs: FileEntry, winner: str) -> None:
    assert pick_winner(mine, theirs) == winner


def test_both_sides_agree_on_the_conflict_outcome() -> None:
    """Whatever side runs the plan, the same version wins under the same name."""
    a_file, b_file = entry("from-A", 1), entry("from-B", 2)
    on_a = plan_files(
        {"f": a_file}, {"f": b_file}, {"f": "0"}, local_side="A", peer_side="B"
    )
    on_b = plan_files(
        {"f": b_file}, {"f": a_file}, {"f": "0"}, local_side="B", peer_side="A"
    )
    (act_a,) = on_a.actions
    (act_b,) = on_b.actions
    assert act_a.winner == "peer" and act_b.winner == "local"  # B's file wins on both
    assert act_a.conflict_path == act_b.conflict_path
