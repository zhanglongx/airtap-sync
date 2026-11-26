from __future__ import annotations

import os
from pathlib import Path

import pytest

from airtap_sync.config import SafetyConfig, parse_config
from airtap_sync.gitsync.cache import RepoCache
from airtap_sync.gitsync.engine import GitSyncAborted
from airtap_sync.gitsync.gitcmd import CombinedAuth, GitAuth
from airtap_sync.gitsync.relay import Relay, RelayContext, RelayOptions
from airtap_sync.gitsync.report import GitSyncReport
from airtap_sync.gitsync.state import GitState
from tests.gitlab.fakegitlab import FakeGitLab
from tests.gitsync.sites import Sites, seed

H = "refs/heads/"
T = "refs/tags/"


class RelayRig:
    """The two simulated GitLabs of ``Sites`` driven by one relay."""

    def __init__(self, sites: Sites, tmp_path: Path) -> None:
        self.sites = sites
        a, b = sites["A"].ctx, sites["B"].ctx
        b.cache = a.cache  # one cache and one base for both directions
        b.state = a.state
        self.ids_b = GitState(tmp_path / "ids_b.sqlite")
        self.rc = RelayContext(a, b, self.ids_b)
        self.staging = tmp_path / "relay-staging"
        self._ids_path = tmp_path / "ids_b.sqlite"

    def reopen(self) -> RelayContext:
        """A fresh context on the same data, as a new command invocation gets."""
        a, b = self.sites["A"].ctx, self.sites["B"].ctx
        cache_dir, size, runner = a.cache.root, a.cache.max_size, a.runner
        a.cache = RepoCache(cache_dir, size, runner)
        a.state = GitState(self.sites["A"].root / "state.sqlite")
        b.cache, b.state = a.cache, a.state
        self.rc = RelayContext(a, b, GitState(self._ids_path))
        return self.rc

    def run(self, *, confirm: bool = False, dry_run: bool = False) -> GitSyncReport:
        return Relay(
            self.rc, self.staging, RelayOptions(confirm=confirm, dry_run=dry_run)
        ).run()


@pytest.fixture
def rig(sites: Sites, tmp_path: Path) -> RelayRig:
    return RelayRig(sites, tmp_path)


def test_changes_flow_both_ways_in_one_run(rig: RelayRig) -> None:
    sites = rig.sites
    seed(sites)
    a, b = sites["A"].remote("team/app"), sites["B"].remote("team/app")
    a.commit("fa", "from A", branch="feature-a")
    b.commit("fb", "from B", branch="feature-b")
    a_tag_target = a.commit("f", "2")
    a.tag("v1")

    report = rig.run()
    assert report.failures == [] and report.relay
    assert sites["A"].heads("team/app") == sites["B"].heads("team/app")
    heads = sites["A"].heads("team/app")
    assert H + "feature-a" in heads and H + "feature-b" in heads
    assert T + "v1" in heads and heads[H + "main"] == a_tag_target

    again = rig.run()  # nothing left to do
    assert again.updated == [] and again.failures == []


def test_deletions_propagate(rig: RelayRig) -> None:
    sites = rig.sites
    seed(sites)
    a = sites["A"].remote("team/app")
    a.commit("g", "x", branch="old")
    rig.run()
    assert H + "old" in sites["B"].heads("team/app")
    a.delete_branch("old")
    report = rig.run()
    assert H + "old" not in sites["B"].heads("team/app")
    assert any("old" in d for d in report.refs_deleted)


def test_diverged_branch_is_parked_on_both_sides(rig: RelayRig) -> None:
    sites = rig.sites
    seed(sites)
    a, b = sites["A"].remote("team/app"), sites["B"].remote("team/app")
    a_tip = a.commit("a", "A")
    b_tip = b.commit("b", "B")
    report = rig.run()
    assert any("refs/heads/main" in c for c in report.conflicts)
    assert sites["A"].heads("team/app")[H + "main"] == a_tip
    assert sites["B"].heads("team/app")[H + "main"] == b_tip
    assert sites["A"].heads("team/app")[H + "airtap/conflict/B/main"] == b_tip
    assert sites["B"].heads("team/app")[H + "airtap/conflict/A/main"] == a_tip
    assert report.needs_attention


def test_new_projects_are_created_on_the_other_network(rig: RelayRig) -> None:
    sites = rig.sites
    sites["A"].add_project("team/from-a", default_branch="dev").commit("f", "1")
    sites["A"].remote("team/from-a").commit("g", "2", branch="dev")
    sites["B"].add_project("team/from-b").commit("f", "b")

    report = rig.run()
    assert sorted(report.created) == ["team/from-a", "team/from-b"]
    assert sites["B"].heads("team/from-a") == sites["A"].heads("team/from-a")
    assert sites["A"].heads("team/from-b") == sites["B"].heads("team/from-b")
    created = sites["B"].project("team/from-a")
    assert created is not None and created["default_branch"] == "dev"

    again = rig.run()
    assert again.created == [] and again.failures == []


def pointer(oid: str, size: int) -> str:
    return (
        f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {size}\n"
    )


def test_lfs_objects_are_copied_server_to_server(rig: RelayRig) -> None:
    sites = rig.sites
    seed(sites)
    content = os.urandom(3000)
    oid, size = sites["A"].gitlab.lfs_put("team/app", content)
    tip = sites["A"].remote("team/app").commit("data.bin", pointer(oid, size))

    report = rig.run()
    assert report.failures == []
    assert sites["B"].heads("team/app")[H + "main"] == tip
    assert sites["B"].gitlab.lfs["team/app"][oid] == content


def test_lfs_into_a_new_project(rig: RelayRig) -> None:
    sites = rig.sites
    remote = sites["A"].add_project("team/new")
    content = os.urandom(1000)
    oid, size = sites["A"].gitlab.lfs_put("team/new", content)
    remote.commit("data.bin", pointer(oid, size))
    report = rig.run()
    assert report.failures == []
    assert sites["B"].gitlab.lfs["team/new"][oid] == content


def test_projects_without_push_rights_are_skipped(rig: RelayRig) -> None:
    sites = rig.sites
    seed(sites)
    sites["B"].gitlab.is_admin = False  # no permissions listed = not a member
    sites["A"].remote("team/app").commit("f", "2")
    before = sites["B"].heads("team/app")
    report = rig.run()
    assert sites["B"].heads("team/app") == before
    assert any("没有推送权限" in w for w in report.warnings)
    assert any("不是管理员" in w for w in report.warnings)


def test_dry_run_changes_nothing(rig: RelayRig) -> None:
    sites = rig.sites
    seed(sites)
    sites["A"].remote("team/app").commit("f", "2")
    sites["A"].add_project("team/extra").commit("f", "x")
    before = sites["B"].heads("team/app")
    report = rig.run(dry_run=True)
    assert report.dry_run and "team/app" in report.updated
    assert any("team/extra" in c for c in report.created)
    assert sites["B"].heads("team/app") == before
    assert sites["B"].project("team/extra") is None


def test_many_new_projects_wait_for_confirmation(
    tmp_path: Path, fake_gitlab: FakeGitLab, gitlab_b: FakeGitLab
) -> None:
    sites = Sites(
        tmp_path, fake_gitlab, gitlab_b, safety=SafetyConfig(max_new_projects=1)
    )
    rig = RelayRig(sites, tmp_path)
    for name in ("one", "two"):
        sites["A"].add_project(f"team/{name}").commit("f", name)
    held = rig.run()
    assert len(held.pending_creates) == 2 and held.has_pending_confirmation
    assert sites["B"].project("team/one") is None
    done = rig.run(confirm=True)
    assert sorted(done.created) == ["team/one", "team/two"]


def test_vanished_projects_abort(
    tmp_path: Path, fake_gitlab: FakeGitLab, gitlab_b: FakeGitLab
) -> None:
    sites = Sites(tmp_path, fake_gitlab, gitlab_b, safety=SafetyConfig(max_deletions=1))
    rig = RelayRig(sites, tmp_path)
    for name in ("p1", "p2", "p3", "keep"):
        seed(sites, f"team/{name}")
    sites["A"].gitlab.projects[:] = [
        p for p in sites["A"].gitlab.projects if p["path"] == "keep"
    ]
    with pytest.raises(GitSyncAborted, match="消失"):
        rig.run()


def test_renames_are_reported_not_applied(rig: RelayRig) -> None:
    sites = rig.sites
    seed(sites)
    rig.run()  # records both sides' project ids
    sites["A"].project("team/app")["path_with_namespace"] = "team/renamed"
    report = rig.run()
    assert any("team/renamed" in n for n in report.notes)
    assert sites["B"].project("team/renamed") is None  # never created as a duplicate


# ----- configuration and authentication ---------------------------------------


def test_relay_config_needs_neither_side_nor_pair_id() -> None:
    config = parse_config(
        {
            "relay": {
                "url_a": "http://a.example/",
                "url_b": "http://b.example",
                "include_projects": ["zh/*"],
                "cache": {"dir": "/c", "max_size": "10GiB"},
            }
        }
    )
    assert config.relay is not None
    assert (config.relay.url_a, config.relay.url_b) == (
        "http://a.example",
        "http://b.example",
    )
    a, b = config.relay.gitlab_config("A"), config.relay.gitlab_config("B")
    assert (a.url, b.url) == ("http://a.example", "http://b.example")
    assert a.include_projects == b.include_projects == ("zh/*",)


@pytest.mark.parametrize(
    ("relay", "message"),
    [
        ({"url_a": "http://x", "cache": {"dir": "/c"}}, "url_b"),
        (
            {"url_a": "http://x", "url_b": "http://X/", "cache": {"dir": "/c"}},
            "不能相同",
        ),
        ({"url_a": "http://x", "url_b": "http://y"}, "relay.cache"),
    ],
)
def test_invalid_relay_config(relay: dict, message: str) -> None:
    from airtap_sync.config import ConfigError

    with pytest.raises(ConfigError, match=message):
        parse_config({"relay": relay})


def test_combined_auth_scopes_each_token_to_its_own_url() -> None:
    env = CombinedAuth(
        [GitAuth("http://a.example", "tok-a"), GitAuth("http://b.example", "tok-b")]
    ).env()
    assert env["GIT_CONFIG_COUNT"] == "4"
    keys = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(4)]
    assert keys[0].startswith("http.http://a.example/") and keys[0].endswith(
        "extraHeader"
    )
    assert keys[2].startswith("http.http://b.example/") and keys[2].endswith(
        "extraHeader"
    )
    assert env["GIT_CONFIG_VALUE_0"] != env["GIT_CONFIG_VALUE_2"]
    assert GitAuth("http://a.example", "t").env()["GIT_CONFIG_COUNT"] == "2"


def test_git_sees_the_combined_credentials() -> None:
    from airtap_sync.gitsync.gitcmd import GitRunner

    runner = GitRunner(
        CombinedAuth(
            [GitAuth("http://a.example", "x"), GitAuth("http://b.example", "y")]
        )
    )
    out = runner.run(["config", "--get-regexp", r"http\..*extraheader"]).stdout
    assert "http://a.example/.extraheader Authorization: Basic" in out
    assert "http://b.example/.extraheader Authorization: Basic" in out


# ----- command line -------------------------------------------------------------------


def relay_config(tmp_path: Path, a: FakeGitLab, b: FakeGitLab) -> Path:
    path = tmp_path / "relay.toml"
    path.write_text(
        f'[relay]\nurl_a = "{a.url}"\nurl_b = "{b.url}"\n'
        f'[relay.cache]\ndir = "{tmp_path / "cache"}"\n',
        encoding="utf-8",
    )
    return path


def test_relay_command(
    rig: RelayRig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from airtap_sync import cli, paths
    from airtap_sync.cli import EXIT_ATTENTION, EXIT_OK, main

    sites = rig.sites
    seed(sites)
    config = relay_config(tmp_path, sites["A"].gitlab, sites["B"].gitlab)
    monkeypatch.setattr(cli, "open_relay_context", lambda _config: rig.reopen())
    monkeypatch.setattr(paths, "staging_dir", lambda: tmp_path / "stage")
    monkeypatch.setattr(paths, "log_dir", lambda: tmp_path / "logs")

    sites["A"].remote("team/app").commit("f", "2")
    assert main(["--config", str(config), "relay"]) == EXIT_OK
    assert "Git：" in capsys.readouterr().out
    assert sites["A"].heads("team/app") == sites["B"].heads("team/app")
    reports = list((tmp_path / "logs" / "reports").glob("*-relay.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "中继" in text and "同步报告" in text

    sites["A"].remote("team/app").commit("a", "A")
    sites["B"].remote("team/app").commit("b", "B")
    assert main(["--config", str(config), "relay"]) == EXIT_ATTENTION  # a conflict


def test_relay_command_needs_a_relay_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from airtap_sync.cli import EXIT_ERROR, main

    config = tmp_path / "c.toml"
    config.write_text('side = "A"\npair_id = "p"\n', encoding="utf-8")
    assert main(["--config", str(config), "relay"]) == EXIT_ERROR
    assert "[relay]" in capsys.readouterr().err


def test_login_asks_for_both_relay_tokens(
    tmp_path: Path,
    fake_gitlab: FakeGitLab,
    gitlab_b: FakeGitLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import getpass

    from airtap_sync import cli
    from airtap_sync.cli import EXIT_OK, main
    from airtap_sync.gitlab.credentials import FileTokenStore

    store = FileTokenStore(tmp_path / "tok.json")
    monkeypatch.setattr(cli, "default_token_store", lambda: store)
    prompts: list[str] = []
    monkeypatch.setattr(
        getpass, "getpass", lambda prompt: prompts.append(prompt) or "secret"
    )
    config = relay_config(tmp_path, fake_gitlab, gitlab_b)
    assert main(["--config", str(config), "gitlab", "login"]) == EXIT_OK
    assert len(prompts) == 2
    assert (
        store.get(fake_gitlab.url) == "secret" and store.get(gitlab_b.url) == "secret"
    )
