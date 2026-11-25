"""The outbox protocol: each side publishes its state under ``outbox/<side>/``.

A run rewrites the outbox in three steps: ``begin`` invalidates the previous
``commit.json``, the caller writes manifests and payload files atomically, and
``commit`` writes ``commit.json`` last. A reader that finds no ``commit.json``,
or one that does not match the manifests on disk, treats the whole outbox as
incomplete and ignores it, so pulling the drive mid-run never yields a wrong
result.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any

from airtap_sync.config import Side
from airtap_sync.medium.atomicio import (
    FileDigest,
    atomic_write_bytes,
    file_digest,
)

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
COMMIT_FILENAME = "commit.json"


class FormatVersionError(Exception):
    """The peer wrote its outbox with an incompatible format version."""


@dataclass(frozen=True)
class ManifestEntry:
    size: int
    sha256: str


@dataclass(frozen=True)
class Commit:
    """Contents of ``commit.json``."""

    format_version: int
    side: Side
    seq: int
    created_at: str
    manifests: dict[str, ManifestEntry] = field(default_factory=dict)

    def to_json(self) -> bytes:
        doc = {
            "format_version": self.format_version,
            "side": self.side,
            "seq": self.seq,
            "created_at": self.created_at,
            "manifests": {
                rel: {"size": m.size, "sha256": m.sha256}
                for rel, m in sorted(self.manifests.items())
            },
        }
        return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def dump_json_gz(obj: Any) -> bytes:
    """Serialise ``obj`` as deterministic gzip-compressed JSON."""
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return gzip.compress(raw.encode("utf-8"), mtime=0)


def load_json_gz(path: Path) -> Any:
    with gzip.open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def resolve_relpath(root: Path, rel: str) -> Path:
    """Map a ``/``-separated relative path under ``root``, rejecting escapes."""
    posix = PurePosixPath(rel)
    bad = (
        not rel
        or posix.is_absolute()
        or "\\" in rel
        or ":" in rel
        or any(part in ("", ".", "..") for part in rel.split("/"))
    )
    if bad:
        raise ValueError(f"unsafe outbox path: {rel!r}")
    return root.joinpath(*posix.parts)


def _parse_commit(data: Any) -> Commit:
    manifests = {
        str(rel): ManifestEntry(int(m["size"]), str(m["sha256"]))
        for rel, m in dict(data["manifests"]).items()
    }
    side = data["side"]
    if side not in ("A", "B"):
        raise ValueError(f"bad side {side!r}")
    return Commit(
        format_version=int(data["format_version"]),
        side=side,
        seq=int(data["seq"]),
        created_at=str(data["created_at"]),
        manifests=manifests,
    )


def read_outbox(outbox_dir: Path, side: Side) -> Commit | None:
    """Return the committed state of an outbox, or ``None`` if unusable.

    ``None`` covers a missing, half-written or corrupted outbox. A different
    ``format_version`` raises ``FormatVersionError`` instead, because that needs
    a human (upgrade the program) rather than a retry.
    """
    commit_file = outbox_dir / COMMIT_FILENAME
    if not commit_file.is_file():
        return None
    try:
        raw = json.loads(commit_file.read_text(encoding="utf-8"))
        version = raw["format_version"]
        if version != FORMAT_VERSION:
            raise FormatVersionError(
                f"对端数据格式版本为 {version}，本程序为 {FORMAT_VERSION}，"
                "请把两端升级到相同版本"
            )
        commit = _parse_commit(raw)
    except FormatVersionError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("outbox %s: unreadable commit.json (%s), ignoring", outbox_dir, e)
        return None

    if commit.side != side:
        log.warning(
            "outbox %s: commit.json belongs to side %s", outbox_dir, commit.side
        )
        return None
    for rel, entry in commit.manifests.items():
        try:
            path = resolve_relpath(outbox_dir, rel)
            digest = file_digest(path)
        except (OSError, ValueError) as e:
            log.warning("outbox %s: manifest %s unusable (%s)", outbox_dir, rel, e)
            return None
        if digest != FileDigest(entry.size, entry.sha256):
            log.warning("outbox %s: manifest %s does not match commit", outbox_dir, rel)
            return None
    return commit


class OutboxWriter:
    """Rewrite one side's outbox; usable as a context manager.

    ``commit.json`` is written only if the ``with`` block exits normally, so an
    exception leaves the outbox invalid (and therefore ignored by the peer).
    """

    def __init__(self, outbox_dir: Path, side: Side) -> None:
        self.dir = outbox_dir
        self.side = side
        self._manifests: dict[str, ManifestEntry] = {}
        self._seq = 0
        self._begun = False
        self.commit_info: Commit | None = None

    def begin(self) -> None:
        """Invalidate the previous commit before touching any content."""
        self.dir.mkdir(parents=True, exist_ok=True)
        commit_file = self.dir / COMMIT_FILENAME
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            self._seq = int(json.loads(commit_file.read_text(encoding="utf-8"))["seq"])
        with contextlib.suppress(FileNotFoundError):
            commit_file.unlink()
        self._manifests = {}
        self._begun = True

    def write_manifest(self, rel: str, data: bytes) -> ManifestEntry:
        """Atomically write a manifest that ``commit.json`` will vouch for."""
        self._require_begun()
        digest = atomic_write_bytes(resolve_relpath(self.dir, rel), data)
        entry = ManifestEntry(digest.size, digest.sha256)
        self._manifests[rel] = entry
        return entry

    def path(self, rel: str) -> Path:
        """Absolute path of a payload file (bundles, blobs) under the outbox."""
        return resolve_relpath(self.dir, rel)

    def commit(self) -> Commit:
        self._require_begun()
        commit = Commit(
            format_version=FORMAT_VERSION,
            side=self.side,
            seq=self._seq + 1,
            created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            manifests=dict(self._manifests),
        )
        atomic_write_bytes(self.dir / COMMIT_FILENAME, commit.to_json())
        self._begun = False
        self.commit_info = commit
        return commit

    def _require_begun(self) -> None:
        if not self._begun:
            raise RuntimeError("OutboxWriter.begin() has not been called")

    def __enter__(self) -> OutboxWriter:
        self.begin()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.commit()
