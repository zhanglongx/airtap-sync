from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from airtap_sync import paths
from airtap_sync.cli import EXIT_OK, main
from airtap_sync.config import FolderConfig
from airtap_sync.filesync.state import FolderState
from airtap_sync.status import folder_status, last_report


def write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    old = time.time() - 3600
    os.utime(path, (old, old))


def test_folder_status_against_the_base(tmp_path: Path) -> None:
    from airtap_sync.medium.atomicio import sha256_file

    root = tmp_path / "docs"
    write(root, "same.txt", "s")
    write(root, "edited.txt", "v1")
    write(root, "gone.txt", "g")
    state_root = tmp_path / "state"
    state = FolderState(state_root / "files" / "docs" / "state.sqlite")
    state.set_base(
        {
            "same.txt": sha256_file(root / "same.txt"),
            "edited.txt": sha256_file(root / "edited.txt"),
            "gone.txt": sha256_file(root / "gone.txt"),
        }
    )
    state.close()

    write(root, "edited.txt", "version two")
    (root / "gone.txt").unlink()
    write(root, "new.txt", "n")
    write(root, "a.sync-conflict-B-20261101-abcd1234.txt", "c")

    status = folder_status(FolderConfig("docs", root), state_root)
    assert status.synced_before
    assert status.modified == ["edited.txt"]
    assert status.deleted == ["gone.txt"]
    assert "new.txt" in status.added
    assert status.conflict_copies == ["a.sync-conflict-B-20261101-abcd1234.txt"]


def test_unavailable_and_never_synced_folders(tmp_path: Path) -> None:
    assert not folder_status(FolderConfig("x", tmp_path / "nope"), tmp_path).available
    root = tmp_path / "d"
    write(root, "a.txt", "a")
    status = folder_status(FolderConfig("d", root), tmp_path / "state")
    assert not status.synced_before and status.added == ["a.txt"]


def test_last_report(tmp_path: Path) -> None:
    assert last_report(tmp_path) is None
    reports = tmp_path / "reports"
    reports.mkdir()
    old, new = reports / "a.md", reports / "b.md"
    old.write_text("x")
    new.write_text("y")
    os.utime(old, (1, 1))
    found = last_report(tmp_path)
    assert found is not None and found[0] == new


def test_status_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    write(data, "a.txt", "alpha")
    config = tmp_path / "config.toml"
    config.write_text(
        f'side = "A"\npair_id = "p"\n[folders.docs]\npath = "{data}"\n'
        f'[gitlab]\nurl = "http://g"\n[gitlab.cache]\ndir = "{tmp_path / "c"}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(paths, "log_dir", lambda: tmp_path / "logs")
    assert main(["--config", str(config), "status"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "本端：A（对端 B）" in out
    assert "还没有同步过" in out
    assert "新增 1 个：a.txt" in out
    assert "还没有基准" in out  # Git part
