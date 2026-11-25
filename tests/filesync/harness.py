"""Two machines with a synchronised folder each, sharing one USB drive."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from airtap_sync.config import FolderConfig, SafetyConfig, Side, TransferConfig
from airtap_sync.filesync.engine import FilesOptions, run_files_sync
from airtap_sync.filesync.report import FilesReport
from airtap_sync.medium.volume import Volume, init_volume


@dataclass
class Machine:
    side: Side
    root: Path  # the synchronised folder
    work: Path  # state and staging
    ignore: tuple[str, ...] = ()

    def write(self, rel: str, content: bytes | str, age_s: float = 3600) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        old = time.time() - age_s
        os.utime(path, (old, old))
        return path

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def files(self) -> dict[str, bytes]:
        return {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in sorted(self.root.rglob("*"))
            if p.is_file() and ".airtap-sync" not in p.parts
        }


@dataclass
class FilePair:
    tmp_path: Path
    transfer: TransferConfig = field(
        default_factory=lambda: TransferConfig(
            max_bytes=10**9, reserve_free=0, part_size=10**6
        )
    )
    safety: SafetyConfig = field(default_factory=SafetyConfig)

    def __post_init__(self) -> None:
        (self.tmp_path / "usb-root").mkdir()
        self.volume: Volume = init_volume(self.tmp_path / "usb-root", "pair")
        self.machines = {
            side: Machine(
                side, self.tmp_path / side / "data", self.tmp_path / side / "w"
            )
            for side in ("A", "B")
        }
        for m in self.machines.values():
            m.root.mkdir(parents=True)

    def __getitem__(self, side: str) -> Machine:
        return self.machines[side]

    def run(
        self, side: str, *, confirm: bool = False, dry_run: bool = False
    ) -> FilesReport:
        m = self.machines[side]
        return run_files_sync(
            {"docs": FolderConfig("docs", m.root, m.ignore)},
            self.volume,
            m.side,
            self.transfer,
            m.work / "staging",
            m.work / "state",
            self.safety,
            FilesOptions(confirm=confirm, dry_run=dry_run),
        )

    def round(self, times: int = 2) -> None:
        """A, B, A, B ...: the first rounds only exchange manifests."""
        for _ in range(times):
            for side in "AB":
                self.run(side)

    def converge(self, limit: int = 8) -> None:
        for _ in range(limit):
            self.round(1)
            if self["A"].files() == self["B"].files():
                self.round(1)
                return
