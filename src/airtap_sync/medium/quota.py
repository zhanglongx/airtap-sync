"""Per-run transfer budget for the USB drive."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from airtap_sync.config import TransferConfig


def compute_budget(max_bytes: int, reserve_free: int, free_bytes: int) -> int:
    """``min(max_bytes, free - reserve)``, never negative."""
    return max(0, min(max_bytes, free_bytes - reserve_free))


@dataclass
class Budget:
    """Bytes that may still be written to the drive in this run."""

    remaining: int
    spent: int = 0

    def try_consume(self, nbytes: int) -> bool:
        """Reserve ``nbytes``; return ``False`` (and reserve nothing) if too big."""
        if nbytes > self.remaining:
            return False
        self.remaining -= nbytes
        self.spent += nbytes
        return True


def usb_budget(volume_root: Path, transfer: TransferConfig) -> Budget:
    """Budget for a run, from the config limits and the drive's free space."""
    free = shutil.disk_usage(volume_root).free
    return Budget(compute_budget(transfer.max_bytes, transfer.reserve_free, free))
