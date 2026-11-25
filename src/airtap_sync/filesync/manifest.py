"""The file manifest each side publishes for one logical folder."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from airtap_sync.config import Side
from airtap_sync.filesync.scanner import FileEntry

MANIFEST_VERSION = 1


def manifest_path(folder: str) -> str:
    return f"files/{folder}.json.gz"


@dataclass
class FolderManifest:
    side: Side
    folder: str
    # Patterns the publishing side ignores; the effective rules are the union.
    ignore: tuple[str, ...] = ()
    files: dict[str, FileEntry] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": MANIFEST_VERSION,
            "side": self.side,
            "folder": self.folder,
            "ignore": list(self.ignore),
            "files": {
                path: [e.size, e.mtime_ns, e.sha256]
                for path, e in sorted(self.files.items())
            },
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> FolderManifest:
        if raw.get("version") != MANIFEST_VERSION:
            raise ValueError(f"unsupported manifest version {raw.get('version')!r}")
        if raw["side"] not in ("A", "B"):
            raise ValueError(f"bad side {raw['side']!r}")
        return cls(
            side=raw["side"],
            folder=str(raw["folder"]),
            ignore=tuple(str(p) for p in raw.get("ignore", [])),
            files={
                str(path): FileEntry(int(size), int(mtime), str(sha))
                for path, (size, mtime, sha) in raw["files"].items()
            },
        )
