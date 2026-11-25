from __future__ import annotations

import os
from pathlib import Path

import pytest

from airtap_sync.gitsync.bundle import (
    BundleError,
    MissingPrerequisites,
    create_bundle,
    import_bundle,
    verify_bundle,
)
from airtap_sync.gitsync.gitcmd import GitRunner
from airtap_sync.gitsync.pusher import push_refs
from airtap_sync.gitsync.refs import ActionKind, RefFilter
from tests.gitsync.gitrepo import Remote, git
from tests.gitsync.pair import PROJECT, Pair

H = "refs/heads/"
T = "refs/tags/"


@pytest.fixture
def pair(tmp_path: Path) -> Pair:
    return Pair(tmp_path)


def test_first_sync_copies_everything(pair: Pair) -> None:
    a = pair["A"].remote
    sha = a.commit("f", "1")
    a.commit("g", "2", branch="dev")
    a.tag("v1")
    result = pair.ship("A", "B")
    assert result.failed == {}
    heads_b = pair.heads("B")
    assert heads_b[H + "main"] == a.rev("main")
    assert heads_b[H + "dev"] == a.rev("dev")
    assert heads_b[T + "v1"] == sha or heads_b[T + "v1"] == a.rev("v1")
    assert pair["B"].base == pair.heads("A")


def test_incremental_bundle_is_smaller_and_applies(pair: Pair) -> None:
    a = pair["A"].remote
    for i in range(30):
        a.commit(f"f{i}", os.urandom(2000).hex())
    pair.sync_both()
    first = pair.bundles[0].stat().st_size
    a.commit("new", "x")
    pair.sync_both()
    assert pair.heads("B")[H + "main"] == a.rev("main")
    assert pair.bundles[-1].stat().st_size < first / 5


def test_independent_branches_on_both_sides_converge(pair: Pair) -> None:
    a, b = pair["A"].remote, pair["B"].remote
    a.commit("f", "1")
    pair.sync_both()
    a.commit("a", "A work", branch="feature-a")
    b.commit("b", "B work", branch="feature-b")
    pair.sync_both()
    pair.sync_both()
    assert pair.heads("A") == pair.heads("B")
    assert H + "feature-a" in pair.heads("B")
    assert H + "feature-b" in pair.heads("A")


def test_fast_forward_of_existing_branch(pair: Pair) -> None:
    a = pair["A"].remote
    a.commit("f", "1")
    pair.sync_both()
    tip = a.commit("f", "2")
    pair.sync_both()
    assert pair.heads("B")[H + "main"] == tip


def test_diverged_branch_is_parked_then_cleaned_after_merge(pair: Pair) -> None:
    a, b = pair["A"].remote, pair["B"].remote
    a.commit("f", "1")
    pair.sync_both()
    a_tip = a.commit("a.txt", "from A")
    b_tip = b.commit("b.txt", "from B")
    pair.sync_both()

    # Nothing was overwritten; each side parked the other's version.
    assert pair.heads("A")[H + "main"] == a_tip
    assert pair.heads("B")[H + "main"] == b_tip
    assert pair.heads("A")[H + "airtap/conflict/B/main"] == b_tip
    assert pair.heads("B")[H + "airtap/conflict/A/main"] == a_tip
    conflicts = pair["A"].last_result
    assert conflicts is not None
    assert [c.kind for c in conflicts.conflicts] == [ActionKind.CONFLICT]

    # Conflict branches do not travel back and forth.
    pair.sync_both()
    assert H + "airtap/conflict/A/main" not in pair.heads("A")
    assert H + "airtap/conflict/B/airtap/conflict/A/main" not in pair.heads("A")

    # A human merges B's version into main on A.
    git("fetch", "-q", "origin", H + "airtap/conflict/B/main", cwd=a.work)
    git("checkout", "-q", "main", cwd=a.work)
    git("merge", "-q", "--no-edit", "FETCH_HEAD", cwd=a.work)
    git("push", "-q", "origin", "main", cwd=a.work)
    merged = a.rev("main")

    pair.sync_both()
    pair.sync_both()
    assert pair.heads("B")[H + "main"] == merged
    assert pair.heads("A")[H + "main"] == merged
    assert H + "airtap/conflict/B/main" not in pair.heads("A")  # cleaned up
    assert H + "airtap/conflict/A/main" not in pair.heads("B")


def test_branch_deletion_propagates_and_is_backed_up(pair: Pair) -> None:
    a = pair["A"].remote
    a.commit("f", "1")
    dev_tip = a.commit("g", "2", branch="dev")
    pair.sync_both()
    assert H + "dev" in pair.heads("B")

    a.delete_branch("dev")
    result = pair.ship("A", "B")
    assert [x.ref for x in result.deletions] == [H + "dev"]
    assert H + "dev" not in pair.heads("B")
    assert pair["B"].base.get(H + "dev") is None
    with pair["B"].cache.open(PROJECT, pair["B"].remote.url) as repo:
        trash = pair.runner.list_refs(repo, "refs/airtap-trash")
    assert list(trash.values()) == [dev_tip]
    assert "20260101T000000Z/heads/dev" in next(iter(trash))


def test_delete_is_skipped_if_server_branch_moved(pair: Pair) -> None:
    a, b = pair["A"].remote, pair["B"].remote
    a.commit("f", "1")
    a.commit("g", "2", branch="dev")
    pair.sync_both()
    a.delete_branch("dev")
    # B's GitLab receives a new commit on dev after B's cache was refreshed.
    runner = pair.runner
    with pair["B"].cache.open(PROJECT, b.url) as repo:
        old = runner.list_refs(repo, "refs/local/heads")["refs/local/heads/dev"]
    b.commit("h", "3", branch="dev")
    result = push_refs(runner, b.bare, b.url, deletes={H + "dev": old})
    assert H + "dev" in result.failed
    assert H + "dev" in pair.heads("B")


def test_tag_conflict_is_reported_and_not_overwritten(pair: Pair) -> None:
    a, b = pair["A"].remote, pair["B"].remote
    a.commit("f", "1")
    pair.sync_both()
    a.commit("f", "2")
    b.commit("f", "other")  # B's main moves independently; keep tags distinct
    a.tag("v1")
    # Tag B's own, different commit with the same name.
    git("tag", "v1", cwd=b.work)
    git("push", "-q", "origin", "v1", cwd=b.work)
    result = pair.ship("A", "B")
    assert [c.kind for c in result.conflicts if c.ref == T + "v1"] == [
        ActionKind.TAG_CONFLICT
    ]
    assert pair.heads("B")[T + "v1"] == b.rev("v1")
    assert pair.heads("A")[T + "v1"] == a.rev("v1")


def test_annotated_tag_on_existing_commit_still_ships(pair: Pair) -> None:
    a = pair["A"].remote
    a.commit("f", "1")
    pair.sync_both()
    a.tag("rel", annotated=True)
    pair.sync_both()
    assert pair.heads("B")[T + "rel"] == a.rev("rel")


def test_nothing_to_send_creates_no_bundle(pair: Pair) -> None:
    a = pair["A"].remote
    a.commit("f", "1")
    pair.sync_both()
    before = len(pair.bundles)
    pair.sync_both()
    assert len(pair.bundles) == before


def test_branch_filter_excludes_refs(pair: Pair) -> None:
    a = pair["A"].remote
    a.commit("f", "1")
    a.commit("w", "wip", branch="wip/x")
    pair.ship("A", "B", ref_filter=RefFilter(exclude_branches=("wip/*",)))
    assert H + "wip/x" not in pair.heads("B")
    assert H + "main" in pair.heads("B")


@pytest.mark.skipif(os.name == "nt", reason="uses a shell hook")
def test_rejected_ref_is_reported_and_others_still_apply(pair: Pair) -> None:
    a, b = pair["A"].remote, pair["B"].remote
    hook = b.bare / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\nwhile read old new ref; do\n"
        '  if [ "$ref" = refs/heads/protected ]; then\n'
        '    echo "protected branch" >&2; exit 1\n'
        "  fi\n"
        "done\nexit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    a.commit("f", "1")
    a.commit("p", "1", branch="protected")
    a.commit("o", "1", branch="open")
    result = pair.ship("A", "B")
    assert H + "protected" in result.failed
    assert "protected branch" in result.failed[H + "protected"]
    assert H + "open" in pair.heads("B")
    assert H + "protected" not in pair["B"].base
    assert H + "open" in pair["B"].base


@pytest.mark.skipif(os.name == "nt", reason="uses a shell hook")
def test_ci_skip_push_option_is_sent(pair: Pair, tmp_path: Path) -> None:
    a, b = pair["A"].remote, pair["B"].remote
    seen = tmp_path / "options.txt"
    hook = b.bare / "hooks" / "pre-receive"
    hook.write_text(f'#!/bin/sh\necho "$GIT_PUSH_OPTION_0" >> {seen}\nexit 0\n')
    hook.chmod(0o755)
    a.commit("f", "1")
    pair.ship("A", "B")
    assert seen.read_text().strip() == "ci.skip"
    a.commit("f", "2")
    pair.ship("A", "B", ci_skip=False)
    assert seen.read_text().splitlines() == ["ci.skip", ""]  # second push: none


def test_push_without_server_push_option_support_falls_back(pair: Pair) -> None:
    a, b = pair["A"].remote, pair["B"].remote
    git("config", "receive.advertisePushOptions", "false", cwd=b.bare)
    a.commit("f", "1")
    result = pair.ship("A", "B")
    assert result.failed == {}
    assert H + "main" in pair.heads("B")


# ----- bundle level ---------------------------------------------------------


def test_missing_prerequisites_are_detected(tmp_path: Path) -> None:
    runner = GitRunner()
    src = Remote(tmp_path, "src")
    first = src.commit("f", "1")
    second = src.commit("f", "2")
    git("update-ref", "refs/local/heads/main", second, cwd=src.bare)
    other = Remote(tmp_path, "other")  # a repo that has neither commit
    other.commit("x", "unrelated")
    ref = "refs/local/heads/main"

    bundle = tmp_path / "inc.bundle"
    assert create_bundle(runner, src.bare, bundle, [ref], [first])
    with pytest.raises(MissingPrerequisites) as exc:
        verify_bundle(runner, other.bare, bundle)
    assert exc.value.missing == [first]
    with pytest.raises(MissingPrerequisites):
        import_bundle(runner, other.bare, bundle)

    full = tmp_path / "full.bundle"
    assert create_bundle(runner, src.bare, full, [ref], [])
    refs = import_bundle(runner, other.bare, full)
    assert refs["refs/peer/heads/main"] == second


def test_bundle_with_unknown_exclusions_and_empty_result(tmp_path: Path) -> None:
    runner = GitRunner()
    src = Remote(tmp_path, "src")
    tip = src.commit("f", "1")
    bundle = tmp_path / "b.bundle"
    # Excluded oids that don't exist locally are ignored.
    assert create_bundle(runner, src.bare, bundle, ["refs/heads/main"], ["0" * 40])
    # Everything already known -> nothing to send, no file left behind.
    empty = tmp_path / "empty.bundle"
    assert not create_bundle(runner, src.bare, empty, ["refs/heads/main"], [tip])
    assert not empty.exists()
    assert not create_bundle(runner, src.bare, empty, [], [])


def test_corrupt_bundle_is_rejected(tmp_path: Path) -> None:
    runner = GitRunner()
    src = Remote(tmp_path, "src")
    src.commit("f", "1")
    bad = tmp_path / "bad.bundle"
    bad.write_bytes(b"not a bundle")
    with pytest.raises(BundleError):
        verify_bundle(runner, src.bare, bad)
