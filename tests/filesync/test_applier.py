from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from airtap_sync.filesync.applier import (
    ApplyError,
    delete_file,
    install_file,
    purge_old_trash,
    trash_path,
    validate_relpath,
)
from airtap_sync.filesync.scanner import FileEntry

DAY = date(2026, 11, 25)
MTIME = 1_700_000_000_000_000_000


def entry_of(path: Path) -> FileEntry:
    st = path.stat()
    return FileEntry(st.st_size, st.st_mtime_ns, "unused")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


def make_source(tmp_path: Path, content: bytes) -> tuple[Path, FileEntry]:
    src = tmp_path / "incoming"
    src.write_bytes(content)
    return src, FileEntry(len(content), MTIME, "unused")


def test_install_creates_file_with_the_peers_mtime(root: Path, tmp_path: Path) -> None:
    src, entry = make_source(tmp_path, b"hello")
    install_file(root, "a/b/c.txt", src, entry, expect=None, day=DAY)
    target = root / "a/b/c.txt"
    assert target.read_bytes() == b"hello"
    assert target.stat().st_mtime_ns == MTIME
    assert not [p for p in root.rglob("*") if p.name.endswith(".tmp")]


def test_install_replaces_and_trashes_the_old_version(
    root: Path, tmp_path: Path
) -> None:
    target = root / "f.txt"
    target.write_bytes(b"old")
    scanned = entry_of(target)
    src, entry = make_source(tmp_path, b"new content")
    install_file(root, "f.txt", src, entry, expect=scanned, day=DAY)
    assert target.read_bytes() == b"new content"
    trashed = root / ".airtap-sync/trash/20261125/f.txt"
    assert trashed.read_bytes() == b"old"


def test_install_refuses_if_the_file_changed_since_the_scan(
    root: Path, tmp_path: Path
) -> None:
    target = root / "f.txt"
    target.write_bytes(b"old")
    scanned = entry_of(target)
    target.write_bytes(b"edited meanwhile!")  # size differs
    src, entry = make_source(tmp_path, b"new")
    with pytest.raises(ApplyError, match="被修改"):
        install_file(root, "f.txt", src, entry, expect=scanned, day=DAY)
    assert target.read_bytes() == b"edited meanwhile!"


def test_install_refuses_if_something_appeared_where_nothing_was(
    root: Path, tmp_path: Path
) -> None:
    (root / "f.txt").write_bytes(b"user's new file")
    src, entry = make_source(tmp_path, b"new")
    with pytest.raises(ApplyError):
        install_file(root, "f.txt", src, entry, expect=None, day=DAY)
    assert (root / "f.txt").read_bytes() == b"user's new file"


def test_install_with_wrong_size_leaves_no_trace(root: Path, tmp_path: Path) -> None:
    src, entry = make_source(tmp_path, b"abc")
    bad = FileEntry(99, MTIME, "x")
    with pytest.raises(ApplyError, match="大小"):
        install_file(root, "f.txt", src, bad, expect=None, day=DAY)
    assert list(root.iterdir()) == []


def test_delete_moves_to_trash_and_removes_empty_dirs(root: Path) -> None:
    f = root / "deep/er/f.txt"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    keep = root / "deep/other.txt"
    keep.write_bytes(b"y")
    delete_file(root, "deep/er/f.txt", expect=entry_of(f), day=DAY)
    assert not (root / "deep/er").exists()  # emptied directory removed
    assert keep.exists()  # a non-empty parent stays
    assert (root / ".airtap-sync/trash/20261125/deep/er/f.txt").read_bytes() == b"x"


def test_delete_refuses_a_modified_file(root: Path) -> None:
    f = root / "f.txt"
    f.write_bytes(b"x")
    scanned = entry_of(f)
    f.write_bytes(b"changed")
    with pytest.raises(ApplyError):
        delete_file(root, "f.txt", expect=scanned, day=DAY)
    assert f.exists()


def test_trash_names_do_not_collide(root: Path) -> None:
    first = trash_path(root, "f.txt", DAY)
    first.parent.mkdir(parents=True)
    first.write_bytes(b"1")
    assert trash_path(root, "f.txt", DAY).name == "f.txt.1"


def test_purge_old_trash(root: Path) -> None:
    for name in ("20260101", "20261110", "20261124", "not-a-date"):
        (root / ".airtap-sync/trash" / name).mkdir(parents=True)
    assert purge_old_trash(root, DAY, days=30) == 1  # only January is older
    left = sorted(p.name for p in (root / ".airtap-sync/trash").iterdir())
    assert left == ["20261110", "20261124", "not-a-date"]


@pytest.mark.parametrize(
    "rel", ["", "/abs", "../x", "a/../b", "a//b", "a\\b", ".airtap-sync/x", "./a"]
)
def test_unsafe_paths_are_rejected_everywhere(rel: str) -> None:
    with pytest.raises(ApplyError):
        validate_relpath(rel, windows=False)


@pytest.mark.parametrize(
    "rel",
    ["a:b.txt", "dir/what?.txt", "CON", "dir/nul.txt", "trailing.", "a|b", "x/y "],
)
def test_windows_names_are_rejected_on_windows_only(rel: str) -> None:
    with pytest.raises(ApplyError):
        validate_relpath(rel, windows=True)
    validate_relpath(rel, windows=False)  # fine on Linux


def test_ordinary_names_pass() -> None:
    for rel in ["a.txt", "dir/sub/file.name.ext", "中文/文件.doc", "a b/c d.txt"]:
        validate_relpath(rel, windows=True)
        validate_relpath(rel, windows=False)
