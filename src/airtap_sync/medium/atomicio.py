"""Crash-safe file writes and hashing helpers.

Every write goes to a temporary sibling first and is moved into place with
``os.replace``, so a reader never observes a half-written file under its final
name even if the drive is pulled mid-write.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

CHUNK_SIZE = 1024 * 1024
TMP_SUFFIX = ".tmp"


@dataclass(frozen=True)
class FileDigest:
    size: int
    sha256: str


def sha256_file(path: Path) -> str:
    return file_digest(path).sha256


def file_digest(path: Path) -> FileDigest:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
            size += len(chunk)
    return FileDigest(size, h.hexdigest())


def tmp_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}{TMP_SUFFIX}")


def atomic_write_stream(path: Path, chunks: Iterable[bytes]) -> FileDigest:
    """Write ``chunks`` to ``path`` atomically and return size and sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(path)
    h = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as f:
            for chunk in chunks:
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return FileDigest(size, h.hexdigest())


def atomic_write_bytes(path: Path, data: bytes) -> FileDigest:
    return atomic_write_stream(path, [data])


def read_chunks(path: Path, offset: int = 0, length: int | None = None):
    """Yield ``length`` bytes (default: to EOF) of ``path`` from ``offset``."""
    remaining = length
    with path.open("rb") as f:
        f.seek(offset)
        while remaining is None or remaining > 0:
            want = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
            chunk = f.read(want)
            if not chunk:
                return
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk


def clean_tmp_files(root: Path) -> int:
    """Remove leftover temporary files from interrupted writes under ``root``."""
    removed = 0
    for tmp in root.rglob(f".*{TMP_SUFFIX}"):
        if tmp.is_file():
            with contextlib.suppress(OSError):
                tmp.unlink()
                removed += 1
    return removed
