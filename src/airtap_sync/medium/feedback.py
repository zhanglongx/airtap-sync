"""Feedback a receiver writes into its own outbox for the sender."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from airtap_sync.medium.outbox import Commit, resolve_relpath

RECEIVED = "received.json"


def load_feedback(peer_dir: Path, commit: Commit | None) -> Received:
    """The peer's feedback about what it already holds of our data."""
    if commit is None or RECEIVED not in commit.manifests:
        return Received()
    return Received.load(resolve_relpath(peer_dir, RECEIVED))


@dataclass
class Received:
    """What this side holds of the peer's data, so the sender can skip it.

    ``staged`` maps a content hash (sha256 of a bundle, LFS object or file) to
    the part indexes already held locally; ``needs_full`` lists projects whose
    incremental bundle could not be applied, for which a full bundle is wanted.
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
