"""Splitting large files into parts and reassembling them on the other side.

Files up to ``part_size`` are stored whole under ``base``. Larger files are
stored as ``base.part0000``, ``base.part0001``, ... plus ``base.parts.json``
holding the whole-file sha256 and one sha256 per part. Parts can therefore be
transferred over several runs (limited by the quota), each one verified on
arrival, and only assembled once complete.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from airtap_sync.medium.atomicio import (
    CHUNK_SIZE,
    atomic_write_bytes,
    atomic_write_stream,
    file_digest,
    read_chunks,
    tmp_path_for,
)
from airtap_sync.medium.quota import Budget

INDEX_SUFFIX = ".parts.json"


class PartsError(Exception):
    """A part or assembled file failed verification, or the source changed."""


@dataclass(frozen=True)
class PartsIndex:
    sha256: str
    size: int
    part_size: int
    part_hashes: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.part_hashes)

    def length(self, index: int) -> int:
        """Byte length of part ``index`` (the last part may be shorter)."""
        return min(self.part_size, self.size - index * self.part_size)

    def to_json(self) -> bytes:
        doc = {
            "sha256": self.sha256,
            "size": self.size,
            "part_size": self.part_size,
            "part_hashes": list(self.part_hashes),
        }
        return (json.dumps(doc, indent=2) + "\n").encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> PartsIndex:
        try:
            doc = json.loads(raw.decode("utf-8"))
            index = cls(
                sha256=str(doc["sha256"]),
                size=int(doc["size"]),
                part_size=int(doc["part_size"]),
                part_hashes=tuple(str(h) for h in doc["part_hashes"]),
            )
        except (ValueError, KeyError, TypeError) as e:
            raise PartsError(f"parts index is malformed: {e}") from None
        expected = -(-index.size // index.part_size) if index.part_size > 0 else -1
        if index.part_size <= 0 or index.count != expected:
            raise PartsError("parts index is inconsistent")
        return index


def part_path(base: Path, index: int) -> Path:
    return base.with_name(f"{base.name}.part{index:04d}")


def index_path(base: Path) -> Path:
    return base.with_name(base.name + INDEX_SUFFIX)


def read_index(base: Path) -> PartsIndex | None:
    path = index_path(base)
    if not path.is_file():
        return None
    return PartsIndex.from_json(path.read_bytes())


def build_index(src: Path, part_size: int) -> PartsIndex:
    """Hash ``src`` once, producing the whole-file and per-part digests."""
    if part_size <= 0:
        raise ValueError("part_size must be positive")
    whole = hashlib.sha256()
    parts: list[str] = []
    size = 0
    current = hashlib.sha256()
    in_part = 0
    with src.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            size += len(chunk)
            whole.update(chunk)
            view = memoryview(chunk)
            while view:
                take = min(len(view), part_size - in_part)
                current.update(view[:take])
                in_part += take
                view = view[take:]
                if in_part == part_size:
                    parts.append(current.hexdigest())
                    current = hashlib.sha256()
                    in_part = 0
    if in_part:
        parts.append(current.hexdigest())
    return PartsIndex(whole.hexdigest(), size, part_size, tuple(parts))


@dataclass(frozen=True)
class StoreResult:
    """Outcome of ``store``: whether the file is now fully on the drive."""

    complete: bool
    parts_written: tuple[int, ...] = ()


def present_parts(base: Path, index: PartsIndex) -> set[int]:
    """Indexes of parts on disk whose size is right (hashes checked on use)."""
    found: set[int] = set()
    for i in range(index.count):
        path = part_path(base, i)
        with contextlib.suppress(OSError):
            if path.stat().st_size == index.length(i):
                found.add(i)
    return found


def store(
    src: Path,
    base: Path,
    *,
    part_size: int,
    budget: Budget | None = None,
    skip: Collection[int] = (),
) -> StoreResult:
    """Place ``src`` on the drive at ``base``, as far as the budget allows.

    ``skip`` lists part indexes the receiver already holds; they are neither
    written nor charged. Bytes already present on the drive are not charged.
    """
    size = src.stat().st_size
    if size <= part_size:
        remove_parts(base, keep_whole=True)  # leftovers of a previously larger file
        with contextlib.suppress(OSError):
            if base.stat().st_size == size:
                return StoreResult(True)
        if budget is not None and not budget.try_consume(size):
            return StoreResult(False)
        digest = atomic_write_stream(base, read_chunks(src))
        if digest.size != size:
            raise PartsError(f"{src} changed while being copied")
        return StoreResult(True, (0,))

    index = build_index(src, part_size)
    existing = None
    with contextlib.suppress(PartsError):
        existing = read_index(base)
    if existing != index:
        # A different version of the file (or none) was staged before.
        remove_parts(base)
        atomic_write_bytes(index_path(base), index.to_json())

    have = present_parts(base, index)
    written: list[int] = []
    complete = True
    for i in range(index.count):
        if i in skip or i in have:
            continue
        if budget is not None and not budget.try_consume(index.length(i)):
            complete = False
            break
        digest = atomic_write_stream(
            part_path(base, i),
            read_chunks(src, i * part_size, index.length(i)),
        )
        if digest.sha256 != index.part_hashes[i]:
            raise PartsError(f"{src} changed while being split")
        written.append(i)
    return StoreResult(complete, tuple(written))


def remove_parts(base: Path, *, keep_whole: bool = False) -> None:
    """Delete a stored file, its parts and its index (all that exist)."""
    doomed = [index_path(base), *base.parent.glob(f"{base.name}.part????")]
    if not keep_whole:
        doomed.append(base)
    for path in doomed:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


@dataclass(frozen=True)
class FetchResult:
    complete: bool
    staged: frozenset[int] = frozenset()


def fetch(
    src_base: Path,
    staging_base: Path,
    dest: Path,
    *,
    sha256: str,
    size: int,
) -> FetchResult:
    """Pull a file from the drive; assemble it at ``dest`` once all parts exist.

    Parts are verified on arrival and kept at ``staging_base`` so a later run
    can continue. ``staged`` is the full set of parts held locally afterwards,
    which the caller reports back to the sender.
    """
    if src_base.is_file():
        digest = atomic_write_stream(dest, read_chunks(src_base))
        if (digest.sha256, digest.size) != (sha256, size):
            with contextlib.suppress(FileNotFoundError):
                dest.unlink()
            raise PartsError(f"{src_base} does not match the expected content")
        return FetchResult(True)

    index = read_index(src_base)
    if index is None:
        return FetchResult(False)
    if (index.sha256, index.size) != (sha256, size):
        raise PartsError(f"parts index of {src_base} does not match the manifest")

    staged = set(valid_staged_parts(staging_base, index))
    for i in range(index.count):
        if i in staged:
            continue
        src = part_path(src_base, i)
        try:
            if src.stat().st_size != index.length(i):
                continue
        except OSError:
            continue
        digest = file_digest(src)
        if digest.sha256 != index.part_hashes[i]:
            raise PartsError(f"{src} is corrupted")
        atomic_write_stream(part_path(staging_base, i), read_chunks(src))
        staged.add(i)

    if len(staged) < index.count:
        return FetchResult(False, frozenset(staged))
    assemble(staging_base, index, dest)
    return FetchResult(True, frozenset(staged))


def valid_staged_parts(staging_base: Path, index: PartsIndex) -> set[int]:
    """Locally staged parts that still match the index."""
    good: set[int] = set()
    for i in present_parts(staging_base, index):
        if file_digest(part_path(staging_base, i)).sha256 == index.part_hashes[i]:
            good.add(i)
    return good


def assemble(staging_base: Path, index: PartsIndex, dest: Path) -> None:
    """Concatenate all staged parts into ``dest`` and verify the whole file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(dest)
    whole = hashlib.sha256()
    try:
        with tmp.open("wb") as out:
            for i in range(index.count):
                for chunk in read_chunks(part_path(staging_base, i)):
                    out.write(chunk)
                    whole.update(chunk)
            out.flush()
            os.fsync(out.fileno())
        if whole.hexdigest() != index.sha256:
            raise PartsError("assembled file does not match the expected sha256")
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    discard_staged(staging_base)


def discard_staged(staging_base: Path) -> None:
    """Delete all staged parts of one file."""
    for path in staging_base.parent.glob(f"{staging_base.name}.part????"):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
