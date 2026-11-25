"""The paired USB volume: ``<root>/airtap-sync/volume.toml`` and its layout."""

from __future__ import annotations

import tomllib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from airtap_sync.config import SIDES, Side
from airtap_sync.medium.atomicio import atomic_write_bytes

VOLUME_DIRNAME = "airtap-sync"
VOLUME_FILENAME = "volume.toml"


class VolumeError(Exception):
    """Raised when a USB volume is missing, invalid or not the expected pair."""


@dataclass(frozen=True)
class Volume:
    """A USB volume initialised for airtap-sync."""

    root: Path
    pair_id: str
    created_at: str

    @property
    def dir(self) -> Path:
        return self.root / VOLUME_DIRNAME

    def outbox_dir(self, side: Side) -> Path:
        return self.dir / "outbox" / side

    @property
    def reports_dir(self) -> Path:
        return self.dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.dir / "logs"

    @property
    def bin_dir(self) -> Path:
        return self.dir / "bin"


def new_pair_id() -> str:
    return str(uuid.uuid4())


def _render(pair_id: str, created_at: str) -> bytes:
    text = (
        "# airtap-sync USB volume marker; do not edit\n"
        f'pair_id = "{pair_id}"\n'
        f'created_at = "{created_at}"\n'
    )
    return text.encode("utf-8")


def init_volume(root: Path, pair_id: str | None = None) -> Volume:
    """Initialise ``root`` as an airtap-sync volume.

    Re-initialising with the same ``pair_id`` is a no-op; with a different one it
    is refused so that two pairs can never be mixed on one drive by accident.
    """
    if not root.is_dir():
        raise VolumeError(f"找不到 U 盘目录：{root}")
    if pair_id is not None and not pair_id.strip():
        raise VolumeError("pair_id 不能为空")
    wanted = pair_id.strip() if pair_id else None

    existing = read_volume(root, required=False)
    if existing is not None:
        if wanted is not None and wanted != existing.pair_id:
            raise VolumeError(
                f"该 U 盘已初始化，pair_id 为 {existing.pair_id}，"
                f"与指定的 {wanted} 不一致"
            )
        return existing

    created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    volume = Volume(root, wanted or new_pair_id(), created_at)
    atomic_write_bytes(
        volume.dir / VOLUME_FILENAME, _render(volume.pair_id, volume.created_at)
    )
    for side in SIDES:
        volume.outbox_dir(side).mkdir(parents=True, exist_ok=True)
    volume.reports_dir.mkdir(exist_ok=True)
    volume.logs_dir.mkdir(exist_ok=True)
    return volume


def read_volume(root: Path, *, required: bool = True) -> Volume | None:
    """Read ``volume.toml``; return ``None`` if absent and not ``required``."""
    marker = root / VOLUME_DIRNAME / VOLUME_FILENAME
    if not marker.is_file():
        if required:
            raise VolumeError(f"{root} 不是已初始化的 U 盘（请先运行 init-usb）")
        return None
    try:
        data = tomllib.loads(marker.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as e:
        raise VolumeError(f"无法读取 {marker}：{e}") from None
    pair_id = data.get("pair_id")
    created_at = data.get("created_at", "")
    if not isinstance(pair_id, str) or not pair_id.strip():
        raise VolumeError(f"{marker} 缺少有效的 pair_id")
    return Volume(root, pair_id, str(created_at))


def open_volume(root: Path, expected_pair_id: str) -> Volume:
    """Open an initialised volume and check it belongs to the configured pair."""
    volume = read_volume(root)
    assert volume is not None
    if volume.pair_id != expected_pair_id:
        raise VolumeError(
            f"U 盘的 pair_id（{volume.pair_id}）与配置中的（{expected_pair_id}）不一致"
        )
    return volume
