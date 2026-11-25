from __future__ import annotations

import os
from pathlib import Path

import pytest

from airtap_sync.config import SafetyConfig, TransferConfig
from airtap_sync.gitsync.engine import GitSyncAborted
from airtap_sync.gitsync.report import render_markdown
from airtap_sync.medium.outbox import FORMAT_VERSION
from tests.gitlab.fakegitlab import FakeGitLab
from tests.gitsync.gitrepo import git
from tests.gitsync.sites import Sites

H = "refs/heads/"
T = "refs/tags/"


@pytest.fixture
def gitlab_b() -> FakeGitLab:
    server = FakeGitLab().start()
    yield server
    server.stop()


@pytest.fixture
def sites(tmp_path: Path, fake_gitlab: FakeGitLab, gitlab_b: FakeGitLab) -> Sites:
    return Sites(tmp_path, fake_gitlab, gitlab_b)


def seed(sites: Sites, path: str = "team/app") -> None:
    """A project that exists on both sides, already in sync (as after a restore)."""
    for side in "AB":
        sites[side].add_project(path)
    a = sites["A"].remote(path)
    a.commit("f", "1")
    git("push", "-q", sites["B"].remote(path).url, "main", cwd=a.work)
    # Both sides adopt the same baseline, as `git snapshot` / `git baseline` would.
    refs = sites["A"].heads(path)
    for side in "AB":
        state = sites[side].ctx.state
        state.add_base_project(path.lower(), path)
        state.set_base_refs(path.lower(), refs)


def test_new_project_is_created_on_the_other_side(sites: Sites) -> None:
    remote = sites["A"].add_project("team/app", default_branch="dev")
    remote.commit("f", "1")
    remote.commit("g", "2", branch="dev")
    remote.tag("v1")

    first = sites.run("A")
    assert first.exported and first.exported[0].full
    assert any("尚未建立基准" in w for w in first.warnings)

    second = sites.run("B")
    assert second.created == ["team/app"]
    created = sites["B"].project("team/app")
    assert created is not None
    assert created["default_branch"] == "dev"
    assert sites["B"].heads("team/app") == sites["A"].heads("team/app")

    sites.run("A")  # A sees that B has everything and stops offering the bundle
    assert not (sites.volume.outbox_dir("A") / "git" / "bundles").exists() or not any(
        (sites.volume.outbox_dir("A") / "git" / "bundles").iterdir()
    )
    report = sites.run("B")
    assert report.created == [] and report.exported == []


def test_incremental_changes_flow_both_ways(sites: Sites) -> None:
    seed(sites)
    a, b = sites["A"].remote("team/app"), sites["B"].remote("team/app")
    a.commit("fa", "A work", branch="feature-a")
    b.commit("fb", "B work", branch="feature-b")
    sites.round()
    assert sites["A"].heads("team/app") == sites["B"].heads("team/app")
    assert H + "feature-a" in sites["B"].heads("team/app")
    assert H + "feature-b" in sites["A"].heads("team/app")

    tip = a.commit("f", "2")
    report = sites.run("A")
    assert [e.full for e in report.exported] == [False]
    sites.run("B")
    assert sites["B"].heads("team/app")[H + "main"] == tip


def test_unchanged_projects_are_not_cloned(sites: Sites) -> None:
    seed(sites)
    sites["A"].add_project("team/idle")
    report = sites.run("A")
    assert report.exported == []
    assert sites["A"].ctx.cache.cached_paths() == []  # nothing needed git objects


def test_fast_forward_and_deletion_propagate(sites: Sites) -> None:
    seed(sites)
    a = sites["A"].remote("team/app")
    a.commit("g", "x", branch="old")
    a.tag("v1")
    sites.round()
    assert H + "old" in sites["B"].heads("team/app")
    a.delete_branch("old")
    report = sites.run("A")
    sites.run("B")
    assert H + "old" not in sites["B"].heads("team/app")
    assert T + "v1" in sites["B"].heads("team/app")
    assert report is not None


def test_diverged_branch_is_reported_as_conflict(sites: Sites) -> None:
    seed(sites)
    a, b = sites["A"].remote("team/app"), sites["B"].remote("team/app")
    a_tip = a.commit("a", "from A")
    b_tip = b.commit("b", "from B")
    sites.run("A")
    report = sites.run("B")
    assert any("refs/heads/main" in c for c in report.conflicts)
    assert sites["B"].heads("team/app")[H + "main"] == b_tip
    assert sites["B"].heads("team/app")[H + "airtap/conflict/A/main"] == a_tip
    assert report.needs_attention
    assert "冲突" in render_markdown(report)


def test_deletions_above_threshold_wait_for_confirmation(
    tmp_path: Path, fake_gitlab: FakeGitLab, gitlab_b: FakeGitLab
) -> None:
    sites = Sites(tmp_path, fake_gitlab, gitlab_b, safety=SafetyConfig(max_deletions=1))
    seed(sites)
    a = sites["A"].remote("team/app")
    for name in ("b1", "b2"):
        a.commit(name, name, branch=name)
    sites.round()
    assert {H + "b1", H + "b2"} <= set(sites["B"].heads("team/app"))

    a.delete_branch("b1")
    a.delete_branch("b2")
    sites.run("A")
    held = sites.run("B")
    assert len(held.pending_deletes) == 2
    assert H + "b1" in sites["B"].heads("team/app")
    assert held.has_pending_confirmation

    sites.run("A")
    done = sites.run("B", confirm=True)
    assert done.pending_deletes == []
    assert H + "b1" not in sites["B"].heads("team/app")
    assert H + "b2" not in sites["B"].heads("team/app")


def test_new_projects_above_threshold_wait_for_confirmation(
    tmp_path: Path, fake_gitlab: FakeGitLab, gitlab_b: FakeGitLab
) -> None:
    sites = Sites(
        tmp_path, fake_gitlab, gitlab_b, safety=SafetyConfig(max_new_projects=1)
    )
    for name in ("one", "two"):
        sites["A"].add_project(f"team/{name}").commit("f", name)
    exported = sites.run("A")
    assert (
        len(exported.pending_creates) == 2
    )  # A also asks before sending 2 full copies
    sites.run("A", confirm=True)
    held = sites.run("B")
    assert len(held.pending_creates) == 2
    assert sites["B"].project("team/one") is None
    done = sites.run("B", confirm=True)
    assert sorted(done.created) == ["team/one", "team/two"]


def test_vanished_project_is_reported_not_recreated(sites: Sites) -> None:
    seed(sites)
    sites.round()
    sites["B"].gitlab.projects.clear()  # deleted (or renamed) on B
    sites["B"].add_project("team/other").commit("f", "x")
    sites.run("B")
    report = sites.run("A")
    assert any("team/app" in n and "不存在" in n for n in report.notes)
    sites.run("B")
    assert sites["B"].project("team/app") is None  # never recreated


def test_batched_transfer_over_several_runs(
    tmp_path: Path, fake_gitlab: FakeGitLab, gitlab_b: FakeGitLab
) -> None:
    sites = Sites(
        tmp_path,
        fake_gitlab,
        gitlab_b,
        transfer=TransferConfig(max_bytes=12_000, reserve_free=0, part_size=4_000),
    )
    seed(sites)
    a = sites["A"].remote("team/app")
    for i in range(3):
        a.commit(f"blob{i}", os.urandom(6_000).hex())
    tip = a.rev("main")

    deferred_seen = False
    for _ in range(8):
        sites.run("A")
        report = sites.run("B")
        deferred_seen |= bool(report.deferred)
        if sites["B"].heads("team/app").get(H + "main") == tip:
            break
    assert deferred_seen
    assert sites["B"].heads("team/app")[H + "main"] == tip


def test_missing_prerequisites_trigger_a_full_bundle(sites: Sites) -> None:
    seed(sites)
    a = sites["A"].remote("team/app")
    # B lost its data (restored from an older backup) but A still assumes the baseline.
    b = sites["B"]
    b.remotes.pop("team/app")
    import shutil

    shutil.rmtree(b.root / "repos")
    b.ctx.cache.clear()
    b.ctx.state.clear()
    tip = a.commit("f", "2")

    sites.run("A")
    first = sites.run("B")
    assert any("完整" in d for d in first.deferred)
    sites.run("A")  # reads B's feedback and sends everything
    sites.run("B")
    assert sites["B"].heads("team/app")[H + "main"] == tip


def test_missing_peer_data_only_exports(sites: Sites) -> None:
    seed(sites)
    report = sites.run("A")
    assert any("尚未收到对方" in w for w in report.warnings)


def test_incomplete_peer_outbox_is_ignored(sites: Sites) -> None:
    seed(sites)
    sites["A"].remote("team/app").commit("f", "2")
    sites.run("A")
    (sites.volume.outbox_dir("A") / "commit.json").unlink()
    report = sites.run("B")
    assert any("尚未收到对方" in w for w in report.warnings)
    assert report.updated == []


def test_zero_projects_aborts_once_projects_were_shared(sites: Sites) -> None:
    sites["A"].ctx.state.set_base_projects({"team/app": "team/app"})
    with pytest.raises(GitSyncAborted, match="没有返回任何工程"):
        sites.run("A")


def test_many_vanished_projects_abort(
    tmp_path: Path, fake_gitlab: FakeGitLab, gitlab_b: FakeGitLab
) -> None:
    sites = Sites(tmp_path, fake_gitlab, gitlab_b, safety=SafetyConfig(max_deletions=1))
    for name in ("p1", "p2", "p3"):
        seed(sites, f"team/{name}")
    sites["B"].add_project("team/keep")  # so B's listing is not empty
    sites["A"].add_project("team/keep")
    sites.run("A")
    sites["A"].gitlab.projects[:] = [
        p for p in sites["A"].gitlab.projects if p["path"] == "keep"
    ]
    sites.run("B")
    with pytest.raises(GitSyncAborted, match="消失"):
        sites.run("A")


def test_dry_run_changes_nothing(sites: Sites) -> None:
    sites["A"].add_project("team/app").commit("f", "1")
    sites.run("A")
    report = sites.run("B", dry_run=True)
    assert report.created == ["team/app"]
    assert report.dry_run
    assert sites["B"].project("team/app") is None
    assert not (sites.volume.outbox_dir("B") / "commit.json").exists()
    assert "试运行" in render_markdown(report)


def test_report_markdown_sections(sites: Sites) -> None:
    sites["A"].add_project("team/app").commit("f", "1")
    sites.run("A")
    report = sites.run("B")
    text = render_markdown(report)
    assert text.startswith("# Git 同步报告")
    assert "## 新建的工程（1）" in text
    assert "- team/app" in text
    assert FORMAT_VERSION == 1


def test_small_quota_spreads_several_projects_over_runs(
    tmp_path: Path, fake_gitlab: FakeGitLab, gitlab_b: FakeGitLab
) -> None:
    sites = Sites(
        tmp_path,
        fake_gitlab,
        gitlab_b,
        transfer=TransferConfig(max_bytes=9_000, reserve_free=0, part_size=4_000),
    )
    names = ("one", "two", "three")
    for name in names:
        seed(sites, f"team/{name}")
    tips = {}
    for name in names:
        tips[name] = (
            sites["A"].remote(f"team/{name}").commit("blob", os.urandom(4_000).hex())
        )

    held_back = False
    for _ in range(12):
        report = sites.run("A")
        held_back |= bool(report.unsent) or any(not e.complete for e in report.exported)
        assert report.failures == []
        failures = sites.run("B").failures
        assert failures == []
        if all(sites["B"].heads(f"team/{n}").get(H + "main") == tips[n] for n in names):
            break
    assert held_back
    for name in names:
        assert sites["B"].heads(f"team/{name}")[H + "main"] == tips[name]


def test_projects_without_push_rights_are_skipped_with_a_warning(
    sites: Sites,
) -> None:
    seed(sites)
    sites["B"].project("team/app")["permissions"] = {
        "project_access": {"access_level": 20},
        "group_access": None,
    }
    sites["B"].gitlab.is_admin = False
    sites["A"].remote("team/app").commit("f", "2")
    sites.run("A")
    before = sites["B"].heads("team/app")
    report = sites.run("B")
    assert sites["B"].heads("team/app") == before
    assert any("无推送权限" in w and "team/app" in w for w in report.warnings)
    assert any("不是管理员" in w for w in report.warnings)
    assert not report.needs_attention  # a known limitation, not an incident


def test_non_members_cannot_receive_unless_admin(sites: Sites) -> None:
    seed(sites)
    sites["A"].remote("team/app").commit("f", "2")
    sites.run("A")
    # A listing without permissions means "visible, but not a member".
    sites["B"].gitlab.is_admin = False
    before = sites["B"].heads("team/app")
    report = sites.run("B")
    assert sites["B"].heads("team/app") == before
    assert any("无推送权限" in w for w in report.warnings)
    sites["B"].gitlab.is_admin = True
    sites.run("A")
    sites.run("B")
    assert sites["B"].heads("team/app") != before


def test_only_included_projects_are_synced(
    tmp_path: Path, fake_gitlab: FakeGitLab, gitlab_b: FakeGitLab
) -> None:
    from dataclasses import replace

    sites = Sites(tmp_path, fake_gitlab, gitlab_b)
    for side in "AB":
        site = sites[side]
        site.ctx.config = replace(site.config, include_projects=("zh/*",))
    sites["A"].add_project("zh/mine").commit("f", "1")
    sites["A"].add_project("other/theirs").commit("f", "2")

    sites.run("A")
    report = sites.run("B")
    assert report.created == ["zh/mine"]
    assert sites["B"].project("other/theirs") is None
