"""Walking a folder: relative paths, sizes, modification times and hashes."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from airtap_sync.filesync.ignore import IgnoreRules
from airtap_sync.filesync.state import FolderState
from airtap_sync.medium.atomicio import sha256_file

# A file modified within this window may be modified again without changing its
# (coarse) mtime, so its hash is not cached yet.
RACY_NS = 2_000_000_000


@dataclass(frozen=True)
class FileEntry:
    size: int
    mtime_ns: int
    sha256: str


@dataclass
class ScanResult:
    files: dict[str, FileEntry] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)  # unreadable files, for the report


def scan_folder(
    root: Path,
    rules: IgnoreRules,
    state: FolderState,
    *,
    now_ns: int | None = None,
) -> ScanResult:
    """Scan ``root`` recursively. Symlinks and special files are skipped.

    Hashes are taken from the cache when size and mtime are unchanged.
    """
    now = time.time_ns() if now_ns is None else now_ns
    result = ScanResult()
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as e:
            result.errors.append(f"{prefix or '.'}：无法读取目录（{e.strerror or e}）")
            continue
        for entry in entries:
            rel = prefix + entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not rules.ignores(rel, is_dir=True):
                        stack.append((Path(entry.path), rel + "/"))
                    continue
                if not entry.is_file(follow_symlinks=False) or rules.ignores(rel):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError as e:
                result.errors.append(f"{rel}：无法读取（{e.strerror or e}）")
                continue
            sha = state.cached_hash(rel, stat.st_size, stat.st_mtime_ns)
            if sha is None:
                try:
                    sha = sha256_file(Path(entry.path))
                except OSError as e:
                    result.errors.append(f"{rel}：无法读取（{e.strerror or e}）")
                    continue
                if now - stat.st_mtime_ns > RACY_NS:
                    state.remember_hash(rel, stat.st_size, stat.st_mtime_ns, sha)
            result.files[rel] = FileEntry(stat.st_size, stat.st_mtime_ns, sha)
    state.forget_hashes_except(result.files)
    state.commit()
    return result
