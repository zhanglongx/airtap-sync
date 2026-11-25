from __future__ import annotations

import getpass
from pathlib import Path

import pytest

from airtap_sync import cli
from airtap_sync.cli import EXIT_ERROR, EXIT_OK, main
from airtap_sync.gitlab.credentials import FileTokenStore
from tests.gitlab.fakegitlab import FakeGitLab


@pytest.fixture
def setup(
    tmp_path: Path, fake_gitlab: FakeGitLab, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, FileTokenStore]:
    config = tmp_path / "config.toml"
    config.write_text(
        f'side = "A"\npair_id = "p"\n[gitlab]\nurl = "{fake_gitlab.url}"\n'
        f'[gitlab.cache]\ndir = "{tmp_path / "cache"}"\n',
        encoding="utf-8",
    )
    store = FileTokenStore(tmp_path / "tok.json")
    monkeypatch.setattr(cli, "default_token_store", lambda: store)
    return config, store


def test_login_saves_valid_token(
    setup: tuple[Path, FileTokenStore],
    fake_gitlab: FakeGitLab,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, store = setup
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "secret")
    assert main(["--config", str(config), "gitlab", "login"]) == EXIT_OK
    assert store.get(fake_gitlab.url) == "secret"
    assert "syncbot" in capsys.readouterr().out


def test_login_rejects_bad_token(
    setup: tuple[Path, FileTokenStore],
    fake_gitlab: FakeGitLab,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, store = setup
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "wrong")
    assert main(["--config", str(config), "gitlab", "login"]) == EXIT_ERROR
    assert store.get(fake_gitlab.url) is None
    assert "令牌验证失败" in capsys.readouterr().err


def test_login_warns_when_not_admin(
    setup: tuple[Path, FileTokenStore],
    fake_gitlab: FakeGitLab,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _ = setup
    fake_gitlab.is_admin = False
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "secret")
    assert main(["--config", str(config), "gitlab", "login"]) == EXIT_OK
    assert "不是管理员" in capsys.readouterr().err


def test_login_without_gitlab_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('side = "A"\npair_id = "p"\n', encoding="utf-8")
    assert main(["--config", str(config), "gitlab", "login"]) == EXIT_ERROR
    assert "[gitlab]" in capsys.readouterr().err
