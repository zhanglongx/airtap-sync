from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from airtap_sync.filesync.ignore import IgnoreRules
from airtap_sync.filesync.scanner import RACY_NS, scan_folder
from airtap_sync.filesync.state import FolderState
from airtap_sync.medium.atomicio import sha256_file


@pytest.fixture
def state(tmp_path: Path) -> FolderState:
    return FolderState(tmp_path / "state" / "s.sqlite")


def write(root: Path, rel: str, content: bytes = b"x", age_s: float = 3600) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def test_scan_lists_files_with_hashes(tmp_path: Path, state: FolderState) -> None:
    root = tmp_path / "root"
    f = write(root, "a/b.txt", b"hello")
    write(root, "top.bin", b"123")
    result = scan_folder(root, IgnoreRules(), state)
    assert set(result.files) == {"a/b.txt", "top.bin"}
    entry = result.files["a/b.txt"]
    assert entry.size == 5
    assert entry.sha256 == sha256_file(f)
    assert entry.mtime_ns == f.stat().st_mtime_ns
    assert result.errors == []


def test_defaults_and_custom_rules_ignore_files(
    tmp_path: Path, state: FolderState
) -> None:
    root = tmp_path / "root"
    for rel in [
        "keep.txt",
        ".git/config",
        "sub/.git/HEAD",
        "~$doc.docx",
        "x.tmp",
        "Thumbs.db",
        ".airtap-sync/trash/20261101/old.txt",
        "build/out.o",
        "logs/a.log",
        "src/main.c",
    ]:
        write(root, rel)
    rules = IgnoreRules(["build/", "*.log"])
    assert set(scan_folder(root, rules, state).files) == {"keep.txt", "src/main.c"}


def test_ignore_rules_union() -> None:
    mine = IgnoreRules(["*.log"])
    both = mine.union(["*.bak"])
    assert both.ignores("a.log") and both.ignores("a.bak") and not mine.ignores("a.bak")
    assert both.ignores("dir/.git", is_dir=True)
    assert both.ignores(".airtap-sync/x") and not both.ignores("a.txt")


def test_symlinks_and_empty_dirs_are_skipped(
    tmp_path: Path, state: FolderState
) -> None:
    root = tmp_path / "root"
    write(root, "real.txt")
    (root / "empty_dir").mkdir()
    try:
        (root / "link.txt").symlink_to(root / "real.txt")
        (root / "linkdir").symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert set(scan_folder(root, IgnoreRules(), state).files) == {"real.txt"}


def test_hashes_come_from_the_cache_when_nothing_changed(
    tmp_path: Path, state: FolderState, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    write(root, "a.txt", b"one")
    scan_folder(root, IgnoreRules(), state)

    from airtap_sync.filesync import scanner

    calls = []
    real = scanner.sha256_file
    monkeypatch.setattr(scanner, "sha256_file", lambda p: calls.append(p) or real(p))
    scan_folder(root, IgnoreRules(), state)
    assert calls == []  # served from the cache

    write(root, "a.txt", b"two!")  # size changes -> rehashed
    again = scan_folder(root, IgnoreRules(), state)
    assert len(calls) == 1
    assert again.files["a.txt"].size == 4


def test_recently_modified_files_are_not_cached(
    tmp_path: Path, state: FolderState
) -> None:
    root = tmp_path / "root"
    write(root, "fresh.txt", b"one", age_s=0)
    scan_folder(root, IgnoreRules(), state)
    st = (root / "fresh.txt").stat()
    assert state.cached_hash("fresh.txt", st.st_size, st.st_mtime_ns) is None
    assert RACY_NS > 0
    # Same size and mtime but different content: must not be mistaken for unchanged.
    (root / "fresh.txt").write_bytes(b"two")
    os.utime(root / "fresh.txt", ns=(st.st_atime_ns, st.st_mtime_ns))
    result = scan_folder(root, IgnoreRules(), state)
    assert result.files["fresh.txt"].sha256 == sha256_file(root / "fresh.txt")


def test_vanished_files_leave_the_cache(tmp_path: Path, state: FolderState) -> None:
    root = tmp_path / "root"
    gone = write(root, "gone.txt")
    write(root, "stay.txt")
    scan_folder(root, IgnoreRules(), state)
    st = gone.stat()
    assert state.cached_hash("gone.txt", st.st_size, st.st_mtime_ns)
    gone.unlink()
    scan_folder(root, IgnoreRules(), state)
    assert state.cached_hash("gone.txt", st.st_size, st.st_mtime_ns) is None


def test_missing_root_reports_an_error(tmp_path: Path, state: FolderState) -> None:
    result = scan_folder(tmp_path / "nope", IgnoreRules(), state)
    assert result.files == {}
    assert result.errors


def test_state_roundtrip(tmp_path: Path) -> None:
    s = FolderState(tmp_path / "x" / "s.sqlite")
    assert s.base() == {}
    s.set_base({"a": "1", "b": "2"})
    assert s.base() == {"a": "1", "b": "2"}
    s.set_base({"a": "3"})
    assert s.base() == {"a": "3"}
    s.close()
    again = FolderState(tmp_path / "x" / "s.sqlite")
    assert again.base() == {"a": "3"}
    again.clear()
    assert again.base() == {}
