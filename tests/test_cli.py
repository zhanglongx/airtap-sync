from __future__ import annotations

import logging
from pathlib import Path

import pytest

from airtap_sync import __version__, paths
from airtap_sync.cli import EXIT_NOT_IMPLEMENTED, build_parser, main
from airtap_sync.logs import setup_logging


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["sync", "--dry-run", "--only", "git"],
        ["status"],
        ["relay"],
        ["git", "snapshot"],
        ["git", "baseline", "base.json"],
        ["git", "reset"],
        ["agent"],
        ["autostart", "enable"],
    ],
)
def test_commands_registered_but_not_implemented(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == EXIT_NOT_IMPLEMENTED
    assert "尚未实现" in capsys.readouterr().err


def test_command_required() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([])
    assert exc.value.code == 2


def test_logging_writes_file(tmp_path: Path) -> None:
    log_file = setup_logging(log_dir=tmp_path / "logs", console=False)
    logging.getLogger("t").info("hello")
    assert "hello" in log_file.read_text(encoding="utf-8")


def test_default_config_file_under_config_dir() -> None:
    assert paths.default_config_file() == paths.config_dir() / "config.toml"
