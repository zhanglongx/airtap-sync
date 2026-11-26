from __future__ import annotations

from pathlib import Path

import pytest

from airtap_sync import paths
from airtap_sync.cli import EXIT_ATTENTION, EXIT_ERROR, EXIT_OK, main
from airtap_sync.medium.volume import init_volume


@pytest.fixture
def machines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    usb = tmp_path / "usb"
    usb.mkdir()
    init_volume(usb, "pair-1")

    def config(side: str) -> Path:
        data = tmp_path / f"data-{side}"
        data.mkdir()
        path = tmp_path / f"config-{side}.toml"
        path.write_text(
            f'side = "{side}"\npair_id = "pair-1"\n[transfer]\nreserve_free = 0\n'
            f'[folders.docs]\npath = "{data.as_posix()}"\nignore = ["*.log"]\n',
            encoding="utf-8",
        )
        return path

    configs = {side: config(side) for side in "AB"}

    def run(side: str, *args: str) -> int:
        monkeypatch.setattr(paths, "state_dir", lambda: tmp_path / f"state-{side}")
        monkeypatch.setattr(paths, "staging_dir", lambda: tmp_path / f"stage-{side}")
        monkeypatch.setattr(paths, "log_dir", lambda: tmp_path / f"logs-{side}")
        return main(["--config", str(configs[side]), "sync", "--usb", str(usb), *args])

    return tmp_path, usb, run


def test_folders_only_configuration(
    machines, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_path, usb, run = machines
    (tmp_path / "data-A" / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "data-A" / "debug.log").write_text("noise", encoding="utf-8")
    for side in "ABAB":
        assert run(side) == EXIT_OK
    assert (tmp_path / "data-B" / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert not (tmp_path / "data-B" / "debug.log").exists()
    out = capsys.readouterr().out
    assert "目录：" in out and "Git：" not in out
    assert list((usb / "airtap-sync" / "reports").glob("*.md"))


def test_conflict_sets_the_attention_exit_code(machines) -> None:
    tmp_path, _usb, run = machines
    (tmp_path / "data-A" / "n.txt").write_text("orig", encoding="utf-8")
    for side in "ABAB":
        run(side)
    (tmp_path / "data-A" / "n.txt").write_text("from A", encoding="utf-8")
    (tmp_path / "data-B" / "n.txt").write_text("from B", encoding="utf-8")
    codes = [run(side) for side in "ABAB"]
    assert EXIT_ATTENTION in codes
    names = {p.name for p in (tmp_path / "data-A").iterdir()}
    assert any(".sync-conflict-" in n for n in names)


def test_dry_run_and_missing_pieces(
    machines, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_path, usb, run = machines
    (tmp_path / "data-A" / "a.txt").write_text("alpha", encoding="utf-8")
    run("A")
    assert run("B", "--dry-run") == EXIT_OK
    assert not (tmp_path / "data-B" / "a.txt").exists()
    assert run("B", "--only", "git") == EXIT_ERROR  # no [gitlab] configured
    assert "[gitlab]" in capsys.readouterr().err
