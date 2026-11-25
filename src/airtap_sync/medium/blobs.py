"""The content-addressed blob area of an outbox (LFS objects and file contents)."""

from __future__ import annotations

import contextlib
from collections.abc import Collection
from pathlib import Path


def blob_base(outbox_dir: Path, sha256: str) -> Path:
    """Base path of a blob (parts append ``.partNNNN``, an index ``.parts.json``)."""
    return outbox_dir / "blobs" / sha256[:2] / sha256


def drop_unwanted_blobs(outbox_dir: Path, wanted: Collection[str]) -> int:
    """Delete blobs whose hash is not in ``wanted``; return how many files went."""
    root = outbox_dir / "blobs"
    removed = 0
    if not root.is_dir():
        return 0
    for shard in root.iterdir():
        for f in shard.iterdir():
            if f.name.split(".", 1)[0] not in wanted:
                f.unlink(missing_ok=True)
                removed += 1
        with contextlib.suppress(OSError):
            shard.rmdir()
    return removed
