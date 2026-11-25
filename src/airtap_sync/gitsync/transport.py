"""Where Git sync data lives inside an outbox, and the receiver's feedback."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from airtap_sync.gitsync.cache import repo_key

GIT_MANIFEST = "git/projects.json.gz"
RECEIVED = "git/received.json"


def bundle_base(outbox_dir: Path, project_path: str, sha256: str) -> Path:
    """Base path of a project's bundle (parts append ``.partNNNN``)."""
    return outbox_dir / "git" / "bundles" / repo_key(project_path) / f"{sha256}.bundle"


def bundle_dir(outbox_dir: Path, project_path: str) -> Path:
    return outbox_dir / "git" / "bundles" / repo_key(project_path)


@dataclass
class Received:
    """Feedback a receiver writes into its own outbox for the sender.

    ``staged`` maps a bundle's sha256 to the part indexes already held, so the
    sender need not resend them; ``needs_full`` lists projects whose incremental
    bundle could not be applied, for which a full bundle is wanted.
    """

    staged: dict[str, list[int]] = field(default_factory=dict)
    needs_full: list[str] = field(default_factory=list)

    def to_bytes(self) -> bytes:
        doc = {
            "staged": {k: sorted(v) for k, v in sorted(self.staged.items())},
            "needs_full": sorted(self.needs_full),
        }
        return (json.dumps(doc, indent=2) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> Received:
        doc = json.loads(raw.decode("utf-8"))
        return cls(
            staged={str(k): [int(i) for i in v] for k, v in doc["staged"].items()},
            needs_full=[str(k) for k in doc["needs_full"]],
        )

    @classmethod
    def load(cls, path: Path) -> Received:
        """Read feedback; missing or unreadable feedback means none."""
        try:
            return cls.from_bytes(path.read_bytes())
        except (OSError, ValueError, KeyError, TypeError):
            return cls()
