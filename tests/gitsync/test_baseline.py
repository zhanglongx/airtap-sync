from __future__ import annotations

import json
from pathlib import Path

import pytest

from airtap_sync.cli import EXIT_ERROR, EXIT_OK, main
from airtap_sync.gitlab.credentials import FileTokenStore
from airtap_sync.gitlab.projects import Project, parse_project
from airtap_sync.gitsync import context
from airtap_sync.gitsync.baseline import (
    Baseline,
    BaselineError,
    adopt_baseline,
    take_snapshot,
)
from airtap_sync.gitsync.cache import RepoCache
from airtap_sync.gitsync.context import GitContext
from airtap_sync.gitsync.gitcmd import GitRunner
from airtap_sync.gitsync.refs import RefFilter
from airtap_sync.gitsync.scan import RemoteRefs
from airtap_sync.gitsync.state import GitState
from tests.gitlab.fakegitlab import FakeGitLab
from tests.gitsync.gitrepo import Remote

H = "refs/heads/"


def project(path: str) -> Project:
    return parse_project(
        {"id": 1, "path": path.rsplit("/", 1)[-1], "path_with_namespace": path}
    )


def refs_ok(path: str, refs: dict[str, str]) -> RemoteRefs:
    return RemoteRefs(path.lower(), refs=refs)


@pytest.fixture
def state(tmp_path: Path) -> GitState:
    return GitState(tmp_path / "state.sqlite")


def test_snapshot_records_projects_and_adopts_them(state: GitState) -> None:
    projects = [project("Team/App"), project("team/lib")]
    refs = {
        "team/app": refs_ok("team/app", {H + "main": "1", H + "airtap/x": "9"}),
        "team/lib": refs_ok("team/lib", {}),
    }
    baseline = take_snapshot("A", projects, refs, RefFilter(), state)
    assert baseline.projects["team/app"] == ("Team/App", {H + "main": "1"})
    assert baseline.projects["team/lib"] == ("team/lib", {})
    assert state.base_projects() == {"team/app": "Team/App", "team/lib": "team/lib"}
    assert state.base_refs("team/app") == {H + "main": "1"}


def test_snapshot_fails_if_any_project_unreadable(state: GitState) -> None:
    projects = [project("t/a"), project("t/b")]
    refs = {"t/a": refs_ok("t/a", {}), "t/b": RemoteRefs("t/b", error="boom")}
    with pytest.raises(BaselineError, match="t/b: boom"):
        take_snapshot("A", projects, refs, RefFilter(), state)
    assert state.base_projects() == {}


def test_baseline_file_roundtrip(tmp_path: Path) -> None:
    baseline = Baseline("A", "2026-11-25T00:00:00Z", {"t/a": ("T/a", {H + "m": "1"})})
    path = tmp_path / "usb" / "baseline.json"
    baseline.save(path)
    assert Baseline.load(path) == baseline


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"version": 9}', '{"version": 1, "side": "A"}']
)
def test_bad_baseline_file(tmp_path: Path, content: str) -> None:
    path = tmp_path / "b.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BaselineError):
        Baseline.load(path)
    with pytest.raises(BaselineError):
        Baseline.load(tmp_path / "missing.json")


def test_adopt_reports_missing_and_changed_projects(state: GitState) -> None:
    baseline = Baseline(
        "A",
        "t",
        {
            "t/ok": ("t/ok", {H + "m": "1"}),
            "t/changed": ("t/changed", {H + "m": "1"}),
            "t/gone": ("t/gone", {H + "m": "1"}),
        },
    )
    local = [project("t/ok"), project("t/changed"), project("t/extra")]
    refs = {
        "t/ok": refs_ok("t/ok", {H + "m": "1"}),
        "t/changed": refs_ok("t/changed", {H + "m": "2"}),
        "t/extra": refs_ok("t/extra", {H + "m": "3"}),
    }
    report = adopt_baseline(baseline, local, refs, RefFilter(), state)
    assert report.adopted == 2
    assert report.missing_locally == ["t/gone"]
    assert report.refs_differ == ["t/changed"]
    # The base is what A recorded, not what B has now, so the difference
    # is later seen as a change and synchronised.
    assert state.base_projects() == {"t/ok": "t/ok", "t/changed": "t/changed"}
    assert state.base_refs("t/changed") == {H + "m": "1"}
    assert state.base_refs("t/extra") == {}


def test_adopt_replaces_previous_base(state: GitState) -> None:
    state.set_base_projects({"old/p": "old/p"})
    adopt_baseline(Baseline("A", "t", {}), [], {}, RefFilter(), state)
    assert state.base_projects() == {}


# ----- CLI -------------------------------------------------------------------------


@pytest.fixture
def env(
    tmp_path: Path, fake_gitlab: FakeGitLab, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Remote]]:
    def write_config(side: str) -> Path:
        path = tmp_path / f"config-{side}.toml"
        path.write_text(
            f'side = "{side}"\npair_id = "p"\n[gitlab]\nurl = "{fake_gitlab.url}"\n'
            f'[gitlab.cache]\ndir = "{(tmp_path / ("cache" + side)).as_posix()}"\n',
            encoding="utf-8",
        )
        return path

    store = FileTokenStore(tmp_path / "tok.json")
    store.set(fake_gitlab.url, "secret")
    monkeypatch.setattr(context, "load_token", lambda url, s=None: store.get(url))
    remotes = {}
    for pid, path in enumerate(["team/a", "team/b"], start=1):
        remotes[path] = Remote(tmp_path, path.replace("/", "-"))
        remotes[path].commit("f", path)
        remotes[path].commit("g", path, branch="dev")
        fake_gitlab.add_project(pid, path)
    monkeypatch.setattr(GitContext, "clone_url", lambda self, path: remotes[path].url)
    write_config("A")
    write_config("B")
    return tmp_path, remotes


def run(tmp_path: Path, side: str, *args: str) -> int:
    return main(["--config", str(tmp_path / f"config-{side}.toml"), *args])


def test_snapshot_then_baseline(
    env: tuple[Path, dict[str, Remote]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path, remotes = env
    out = tmp_path / "usb" / "baseline.json"
    assert run(tmp_path, "A", "git", "snapshot", "--output", str(out)) == EXIT_OK
    assert "已记录 2 个工程" in capsys.readouterr().out
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["side"] == "A"
    team_a = next(p for p in doc["projects"] if p["path"] == "team/a")
    assert team_a["refs"] == {
        H + "main": remotes["team/a"].rev("main"),
        H + "dev": remotes["team/a"].rev("dev"),
    }

    # Same side cannot adopt its own snapshot.
    assert run(tmp_path, "A", "git", "baseline", str(out)) == EXIT_ERROR
    assert "对端" in capsys.readouterr().err

    assert run(tmp_path, "B", "git", "baseline", str(out)) == EXIT_OK
    captured = capsys.readouterr()
    assert "已采用基准：2 个工程" in captured.out
    assert "警告" not in captured.err

    # A branch changed after the backup is reported as a difference.
    remotes["team/a"].commit("h", "later", branch="dev")
    assert run(tmp_path, "B", "git", "baseline", str(out)) == EXIT_OK
    assert "team/a" in capsys.readouterr().err


def test_reset(env: tuple[Path, dict[str, Remote]], capsys) -> None:
    tmp_path, _ = env
    out = tmp_path / "baseline.json"
    run(tmp_path, "A", "git", "snapshot", "--output", str(out))
    run(tmp_path, "A", "git", "warm", "--days", "36500")
    cache = RepoCache(tmp_path / "cacheA", 10**9, GitRunner())
    assert cache.cached_paths()
    cache.close()

    assert run(tmp_path, "A", "git", "reset", "--yes") == EXIT_OK
    cache = RepoCache(tmp_path / "cacheA", 10**9, GitRunner())
    assert cache.cached_paths() == []
    cache.close()
    state = GitState(tmp_path / "state" / "git" / "state.sqlite")
    assert state.base_projects() == {}


def test_reset_asks_for_confirmation(
    env: tuple[Path, dict[str, Remote]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_path, _ = env
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert run(tmp_path, "A", "git", "reset") == EXIT_ERROR
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert run(tmp_path, "A", "git", "reset") == EXIT_OK
