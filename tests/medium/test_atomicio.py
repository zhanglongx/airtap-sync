from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from airtap_sync.medium.atomicio import (
    atomic_write_bytes,
    atomic_write_stream,
    clean_tmp_files,
    file_digest,
    read_chunks,
)


def test_write_and_digest(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "a.bin"
    digest = atomic_write_bytes(target, b"hello")
    assert target.read_bytes() == b"hello"
    assert digest.size == 5
    assert digest.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert file_digest(target) == digest


def test_failed_write_leaves_no_file(tmp_path: Path) -> None:
    target = tmp_path / "a.bin"
    target.write_bytes(b"old")

    def chunks():
        yield b"new"
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        atomic_write_stream(target, chunks())
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_read_chunks_range(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_bytes(bytes(range(100)))
    assert b"".join(read_chunks(f, 10, 5)) == bytes(range(10, 15))
    assert b"".join(read_chunks(f, 95)) == bytes(range(95, 100))


def test_clean_tmp_files(tmp_path: Path) -> None:
    (tmp_path / ".x.1234abcd.tmp").write_bytes(b"x")
    (tmp_path / "keep.bin").write_bytes(b"y")
    assert clean_tmp_files(tmp_path) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["keep.bin"]
