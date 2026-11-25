from __future__ import annotations

from pathlib import Path

import pytest

from airtap_sync.cli import EXIT_ERROR, EXIT_OK, main
from airtap_sync.gitlab.credentials import FileTokenStore
from airtap_sync.gitsync import context
from airtap_sync.gitsync.cache import RepoCache
from airtap_sync.gitsync.context import GitContext
from airtap_sync.gitsync.gitcmd import GitRunner
from tests.gitlab.fakegitlab import FakeGitLab
from tests.gitsync.gitrepo import Remote


@pytest.fixture
def env(
    tmp_path: Path, fake_gitlab: FakeGitLab, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Remote]]:
    config = tmp_path / "config.toml"
    config.write_text(
        f'side = "A"\npair_id = "p"\n[gitlab]\nurl = "{fake_gitlab.url}"\n'
        'exclude_projects = ["sandbox/*"]\n'
        f'[gitlab.cache]\ndir = "{tmp_path / "cache"}"\n',
        encoding="utf-8",
    )
    store = FileTokenStore(tmp_path / "tok.json")
    store.set(fake_gitlab.url, "secret")
    monkeypatch.setattr(context, "load_token", lambda url, s=None: store.get(url))
    remotes = {}
    for pid, path in enumerate(["team/a", "team/b", "sandbox/c"], start=1):
        remotes[path] = Remote(tmp_path, path.replace("/", "-"))
        remotes[path].commit("f", path)
        fake_gitlab.add_project(pid, path, statistics={"repository_size": 1000})
    monkeypatch.setattr(GitContext, "clone_url", lambda self, path: remotes[path].url)
    return config, remotes


def test_git_warm_clones_recent_projects(
    env: tuple[Path, dict[str, Remote]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _ = env
    code = main(["--config", str(config), "git", "warm", "--days", "36500"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "新克隆 2 个" in out

    cache = RepoCache(tmp_path / "cache", 10**9, GitRunner())
    assert cache.cached_paths() == ["team/a", "team/b"]  # sandbox/* excluded

    assert main(["--config", str(config), "git", "warm", "--days", "36500"]) == EXIT_OK
    assert "已在缓存 2 个" in capsys.readouterr().out


def test_git_warm_reports_failures(
    env: tuple[Path, dict[str, Remote]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, remotes = env
    monkeypatch.setattr(
        GitContext,
        "clone_url",
        lambda self, path: (
            (tmp_path / "gone.git").as_uri() if path == "team/b" else remotes[path].url
        ),
    )
    assert (
        main(["--config", str(config), "git", "warm", "--days", "36500"]) == EXIT_ERROR
    )
    err = capsys.readouterr().err
    assert "team/b" in err


def test_git_warm_without_token(
    env: tuple[Path, dict[str, Remote]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = env
    monkeypatch.setattr(context, "load_token", lambda url, s=None: None)
    assert main(["--config", str(config), "git", "warm"]) == EXIT_ERROR
    assert "gitlab login" in capsys.readouterr().err
