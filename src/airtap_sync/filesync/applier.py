"""Changing files safely: temp file + replace, trash instead of delete."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from datetime import date, timedelta
from pathlib import Path

from airtap_sync.filesync.ignore import INTERNAL_DIR
from airtap_sync.filesync.scanner import FileEntry
from airtap_sync.medium.atomicio import tmp_path_for

TRASH_DAYS = 30
_WINDOWS_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class ApplyError(Exception):
    """A file could not be changed; the message is shown in the report."""


def validate_relpath(rel: str, *, windows: bool | None = None) -> None:
    """Reject paths that must never be written (escapes, illegal names)."""
    windows = os.name == "nt" if windows is None else windows
    parts = rel.split("/")
    if (
        not rel
        or rel.startswith("/")
        or "\\" in rel
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise ApplyError(f"不安全的路径：{rel!r}")
    if parts[0] == INTERNAL_DIR:
        raise ApplyError(f"路径指向程序自用目录：{rel}")
    if windows:
        for part in parts:
            if _WINDOWS_BAD_CHARS.search(part):
                raise ApplyError(f"Windows 不允许的文件名：{rel}")
            if part.split(".")[0].lower() in _WINDOWS_RESERVED:
                raise ApplyError(f"Windows 保留的文件名：{rel}")
            if part.endswith((" ", ".")):
                raise ApplyError(f"Windows 不允许以空格或点结尾：{rel}")


def trash_path(root: Path, rel: str, day: date) -> Path:
    """A free location for ``rel`` in the trash of ``day``."""
    base = root / INTERNAL_DIR / "trash" / day.strftime("%Y%m%d") / rel
    candidate, n = base, 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}.{n}")
        n += 1
    return candidate


def _unchanged(path: Path, expect: FileEntry | None) -> bool:
    """Whether ``path`` still looks like the scanned ``expect`` (or is absent)."""
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return expect is None
    except OSError:
        return False
    return (
        expect is not None
        and stat.st_size == expect.size
        and stat.st_mtime_ns == expect.mtime_ns
    )


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except PermissionError:
        raise ApplyError("文件被占用，留待下一轮") from None


def _remove_empty_parents(root: Path, rel: str) -> None:
    parent = (root / rel).parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            return  # not empty (or not removable): stop
        parent = parent.parent


def install_file(
    root: Path,
    rel: str,
    source: Path,
    entry: FileEntry,
    *,
    expect: FileEntry | None,
    day: date,
) -> None:
    """Put ``source``'s content at ``root/rel`` with the peer's mtime.

    ``expect`` is the local file as scanned (``None`` = it did not exist). If
    the file changed since, nothing is touched. A replaced file goes to the
    trash first and is restored if the final step fails.
    """
    validate_relpath(rel)
    dest = root / rel
    if not _unchanged(dest, expect):
        raise ApplyError("文件在同步过程中被修改或已存在，留待下一轮")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(dest)
    try:
        shutil.copyfile(source, tmp)
        if tmp.stat().st_size != entry.size:
            raise ApplyError("写入的大小与预期不符")
        os.utime(tmp, ns=(entry.mtime_ns, entry.mtime_ns))
        trashed: Path | None = None
        if expect is not None:
            trashed = trash_path(root, rel, day)
            _move(dest, trashed)
        try:
            os.replace(tmp, dest)
        except OSError as e:
            if trashed is not None:
                with contextlib.suppress(OSError):
                    os.replace(trashed, dest)
            if isinstance(e, PermissionError):
                raise ApplyError("文件被占用，留待下一轮") from None
            raise ApplyError(f"无法写入：{e.strerror or e}") from None
    except OSError as e:
        raise ApplyError(f"无法写入：{e.strerror or e}") from None
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def delete_file(root: Path, rel: str, *, expect: FileEntry, day: date) -> None:
    """Move ``root/rel`` to the trash (if unchanged) and tidy empty directories."""
    validate_relpath(rel)
    dest = root / rel
    if not _unchanged(dest, expect):
        raise ApplyError("文件在同步过程中被修改，留待下一轮")
    _move(dest, trash_path(root, rel, day))
    _remove_empty_parents(root, rel)


def purge_old_trash(root: Path, today: date, days: int = TRASH_DAYS) -> int:
    """Delete trash folders older than ``days``; return how many were removed."""
    trash = root / INTERNAL_DIR / "trash"
    if not trash.is_dir():
        return 0
    cutoff = today - timedelta(days=days)
    removed = 0
    for entry in trash.iterdir():
        try:
            day = date(int(entry.name[:4]), int(entry.name[4:6]), int(entry.name[6:8]))
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed
