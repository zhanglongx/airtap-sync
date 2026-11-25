"""Finding the paired USB drive among the mounted volumes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import psutil

from airtap_sync.medium.volume import Volume, VolumeError, read_volume

log = logging.getLogger(__name__)


def mounted_roots() -> list[Path]:
    """Mount points of the real (non-pseudo) filesystems."""
    roots = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue  # an empty optical drive on Windows has no filesystem
        roots.append(Path(part.mountpoint))
    return roots


def find_volumes(
    pair_id: str, roots: Callable[[], list[Path]] = mounted_roots
) -> dict[Path, Volume]:
    """Initialised volumes among the mounts whose ``pair_id`` matches."""
    found: dict[Path, Volume] = {}
    for root in roots():
        try:
            volume = read_volume(root, required=False)
        except (VolumeError, OSError) as e:
            log.debug("cannot read volume marker on %s: %s", root, e)
            continue
        if volume is not None and volume.pair_id == pair_id:
            found[root] = volume
    return found
