from __future__ import annotations

from pathlib import Path

import pytest

from airtap_sync.cli import EXIT_ERROR, EXIT_OK, main
from airtap_sync.medium.volume import (
    VolumeError,
    init_volume,
    open_volume,
    read_volume,
)


def test_init_creates_layout(tmp_path: Path) -> None:
    volume = init_volume(tmp_path)
    assert (volume.dir / "volume.toml").is_file()
    assert volume.outbox_dir("A").is_dir()
    assert volume.outbox_dir("B").is_dir()
    assert volume.reports_dir.is_dir()
    again = read_volume(tmp_path)
    assert again is not None
    assert again.pair_id == volume.pair_id
    assert again.created_at == volume.created_at


def test_reinit_is_idempotent(tmp_path: Path) -> None:
    first = init_volume(tmp_path)
    assert init_volume(tmp_path) == first
    assert init_volume(tmp_path, first.pair_id) == first


def test_reinit_with_other_pair_id_refused(tmp_path: Path) -> None:
    init_volume(tmp_path)
    with pytest.raises(VolumeError, match="不一致"):
        init_volume(tmp_path, "other")


def test_init_with_given_pair_id(tmp_path: Path) -> None:
    assert init_volume(tmp_path, " my-pair ").pair_id == "my-pair"


def test_init_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(VolumeError, match="找不到"):
        init_volume(tmp_path / "nope")


def test_read_uninitialised(tmp_path: Path) -> None:
    assert read_volume(tmp_path, required=False) is None
    with pytest.raises(VolumeError, match="init-usb"):
        read_volume(tmp_path)


def test_read_corrupt_marker(tmp_path: Path) -> None:
    volume = init_volume(tmp_path)
    (volume.dir / "volume.toml").write_text("pair_id = ", encoding="utf-8")
    with pytest.raises(VolumeError, match="无法读取"):
        read_volume(tmp_path)
    (volume.dir / "volume.toml").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(VolumeError, match="pair_id"):
        read_volume(tmp_path)


def test_open_volume_checks_pair(tmp_path: Path) -> None:
    volume = init_volume(tmp_path)
    assert open_volume(tmp_path, volume.pair_id) == volume
    with pytest.raises(VolumeError, match="不一致"):
        open_volume(tmp_path, "someone-else")


def test_cli_init_usb(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init-usb", str(tmp_path)]) == EXIT_OK
    out = capsys.readouterr().out
    volume = read_volume(tmp_path)
    assert volume is not None
    assert volume.pair_id in out
    assert main(["init-usb", str(tmp_path), "--pair-id", "zzz"]) == EXIT_ERROR
    assert "不一致" in capsys.readouterr().err
