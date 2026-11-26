from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from airtap_sync import paths
from airtap_sync.cli import (
    EXIT_ATTENTION,
    EXIT_ERROR,
    EXIT_OK,
    main,
)
from airtap_sync.gitlab.credentials import FileTokenStore
from airtap_sync.gitsync import context
from airtap_sync.gitsync.context import GitContext
from airtap_sync.medium.volume import init_volume
from tests.gitlab.fakegitlab import FakeGitLab
from tests.gitsync.gitrepo import Remote, git

H = "refs/heads/"


class Env:
    """Two machines driven through the real CLI, sharing one USB drive."""

    def __init__(
        self,
        tmp_path: Path,
        gitlabs: dict[str, FakeGitLab],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.tmp_path = tmp_path
        self.gitlabs = gitlabs
        self.monkeypatch = monkeypatch
        self.usb = tmp_path / "usb"
        self.usb.mkdir()
        init_volume(self.usb, "pair-1")
        self.remotes: dict[str, dict[str, Remote]] = {"A": {}, "B": {}}
        self.configs: dict[str, Path] = {}
        for side, gitlab in gitlabs.items():
            config = tmp_path / f"config-{side}.toml"
            config.write_text(
                f'side = "{side}"\npair_id = "pair-1"\n'
                f"[transfer]\nreserve_free = 0\n"
                f'[gitlab]\nurl = "{gitlab.url}"\n'
                f'[gitlab.cache]\ndir = "{(tmp_path / ("cache" + side)).as_posix()}"\n',
                encoding="utf-8",
            )
            self.configs[side] = config
            store = FileTokenStore(tmp_path / f"tok-{side}.json")
            store.set(gitlab.url, "secret")
            self.stores = getattr(self, "stores", {})
            self.stores[gitlab.url] = store

    def remote(self, side: str, path: str) -> Remote:
        remotes = self.remotes[side]
        if path not in remotes:
            (self.tmp_path / f"repos-{side}").mkdir(exist_ok=True)
            remote = Remote(self.tmp_path / f"repos-{side}", path.replace("/", "__"))
            git("config", "receive.advertisePushOptions", "true", cwd=remote.bare)
            remotes[path] = remote
        return remotes[path]

    def add_project(self, side: str, path: str) -> Remote:
        self.gitlabs[side].add_project(len(self.gitlabs[side].projects) + 1, path)
        return self.remote(side, path)

    def run(self, side: str, *args: str) -> int:
        mp = self.monkeypatch
        mp.setattr(paths, "state_dir", lambda: self.tmp_path / f"state-{side}")
        mp.setattr(paths, "staging_dir", lambda: self.tmp_path / f"staging-{side}")
        mp.setattr(paths, "log_dir", lambda: self.tmp_path / f"logs-{side}")
        mp.setattr(
            GitContext, "clone_url", lambda self_, path: self.remote(side, path).url
        )
        mp.setattr(context, "load_token", lambda url, s=None: self.stores[url].get(url))
        return main(["--config", str(self.configs[side]), *args])


@pytest.fixture
def env(
    tmp_path: Path, fake_gitlab: FakeGitLab, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Env]:
    other = FakeGitLab().start()
    try:
        yield Env(tmp_path, {"A": fake_gitlab, "B": other}, monkeypatch)
    finally:
        other.stop()


def test_sync_creates_project_and_writes_reports(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    remote = env.add_project("A", "team/app")
    remote.commit("f", "1")

    assert env.run("A", "sync", "--usb", str(env.usb)) == EXIT_OK
    assert env.run("B", "sync", "--usb", str(env.usb)) == EXIT_OK
    out = capsys.readouterr().out
    assert "新建 1" in out
    assert env.remote("B", "team/app").rev("main") == remote.rev("main")

    usb_reports = sorted((env.usb / "airtap-sync" / "reports").glob("*.md"))
    assert [p.stem.split("-")[2] for p in usb_reports] == ["A", "B"]
    local_reports = list((env.tmp_path / "logs-B" / "reports").glob("*.md"))
    assert len(local_reports) == 1
    assert "team/app" in local_reports[0].read_text(encoding="utf-8")


def test_conflict_gives_attention_exit_code(env: Env) -> None:
    a = env.add_project("A", "team/app")
    a.commit("f", "1")
    env.run("A", "sync", "--usb", str(env.usb))
    env.run("B", "sync", "--usb", str(env.usb))
    a.commit("a", "A")
    env.remote("B", "team/app").commit("b", "B")
    env.run("A", "sync", "--usb", str(env.usb))
    assert env.run("B", "sync", "--usb", str(env.usb)) == EXIT_ATTENTION


def test_dry_run_leaves_no_trace(env: Env) -> None:
    env.add_project("A", "team/app").commit("f", "1")
    env.run("A", "sync", "--usb", str(env.usb))
    assert env.run("B", "sync", "--usb", str(env.usb), "--dry-run") == EXIT_OK
    assert env.gitlabs["B"].projects == []


def test_pending_creations_need_confirmation(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    for config in env.configs.values():
        with config.open("a", encoding="utf-8") as f:
            f.write("[safety]\nmax_new_projects = 1\n")
    for name in ("one", "two"):
        env.add_project("A", f"team/{name}").commit("f", name)
    env.run("A", "sync", "--usb", str(env.usb), "--confirm-deletes")

    assert env.run("B", "sync", "--usb", str(env.usb)) == EXIT_ATTENTION
    assert env.gitlabs["B"].projects == []
    assert "需要确认" in capsys.readouterr().err

    assert env.run("B", "sync", "--usb", str(env.usb), "--confirm-deletes") == EXIT_OK
    assert len(env.gitlabs["B"].projects) == 2


def test_interactive_confirmation(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    for config in env.configs.values():
        with config.open("a", encoding="utf-8") as f:
            f.write("[safety]\nmax_new_projects = 1\n")
    for name in ("one", "two"):
        env.add_project("A", f"team/{name}").commit("f", name)
    env.run("A", "sync", "--usb", str(env.usb), "--confirm-deletes")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert env.run("B", "sync", "--usb", str(env.usb)) == EXIT_OK
    assert len(env.gitlabs["B"].projects) == 2


def test_errors(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("A", "sync") == EXIT_ERROR
    assert "--usb" in capsys.readouterr().err

    assert env.run("A", "sync", "--usb", str(env.tmp_path / "nowhere")) == EXIT_ERROR
    assert "同步中止" in capsys.readouterr().err

    other = env.tmp_path / "other-usb"
    other.mkdir()
    init_volume(other, "different-pair")
    assert env.run("A", "sync", "--usb", str(other)) == EXIT_ERROR
    assert "pair_id" in capsys.readouterr().err

    assert env.run("A", "sync", "--usb", str(env.usb), "--only", "files") == EXIT_ERROR
    assert "folders" in capsys.readouterr().err


def test_files_and_git_are_synchronised_in_one_run(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    for side in "AB":
        data = env.tmp_path / f"data-{side}"
        data.mkdir()
        with env.configs[side].open("a", encoding="utf-8") as f:
            f.write(f'\n[folders.docs]\npath = "{data.as_posix()}"\n')
    env.add_project("A", "team/app").commit("f", "1")
    (env.tmp_path / "data-A" / "note.txt").write_text("hello", encoding="utf-8")

    usb = str(env.usb)
    for side in "ABAB":
        assert env.run(side, "sync", "--usb", usb) in (EXIT_OK, EXIT_ATTENTION)
    out = capsys.readouterr().out
    assert "Git：" in out and "目录：" in out

    assert (env.tmp_path / "data-B" / "note.txt").read_text(encoding="utf-8") == "hello"
    assert env.remote("B", "team/app").rev("main")
    commit = (env.usb / "airtap-sync" / "outbox" / "B" / "commit.json").read_text()
    assert "git/projects.json.gz" in commit and "files/docs.json.gz" in commit
    report = sorted((env.usb / "airtap-sync" / "reports").glob("*-B.md"))[-1]
    text = report.read_text(encoding="utf-8")
    assert "## Git 同步" in text and "## 目录同步" in text


def test_only_files_keeps_the_git_manifest(
    env: Env,
) -> None:
    data = env.tmp_path / "data-A"
    data.mkdir()
    with env.configs["A"].open("a", encoding="utf-8") as f:
        f.write(f'\n[folders.docs]\npath = "{data.as_posix()}"\n')
    env.add_project("A", "team/app").commit("f", "1")
    usb = str(env.usb)
    assert env.run("A", "sync", "--usb", usb) in (EXIT_OK, EXIT_ATTENTION)
    commit = env.usb / "airtap-sync" / "outbox" / "A" / "commit.json"
    assert "git/projects.json.gz" in commit.read_text()

    (data / "x.txt").write_text("x", encoding="utf-8")
    assert env.run("A", "sync", "--usb", usb, "--only", "files") == EXIT_OK
    text = commit.read_text()
    assert "git/projects.json.gz" in text and "files/docs.json.gz" in text
