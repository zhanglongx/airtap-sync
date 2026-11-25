"""Per-folder persistent state: the agreed base and the hash cache (sqlite)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path

_SCHEMA = """
-- Content hash of every file seen on the last scan; valid while the size and
-- the modification time are unchanged, which saves re-reading large folders.
CREATE TABLE IF NOT EXISTS hashes (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL
);

-- sha256 of each file both sides were last seen to agree on (the three-way base).
CREATE TABLE IF NOT EXISTS base (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL
);
"""


class FolderState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ----- hash cache ------------------------------------------------------------

    def cached_hash(self, rel: str, size: int, mtime_ns: int) -> str | None:
        row = self._db.execute(
            "SELECT sha256 FROM hashes WHERE path = ? AND size = ? AND mtime_ns = ?",
            (rel, size, mtime_ns),
        ).fetchone()
        return None if row is None else str(row[0])

    def remember_hash(self, rel: str, size: int, mtime_ns: int, sha256: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO hashes (path, size, mtime_ns, sha256) "
            "VALUES (?, ?, ?, ?)",
            (rel, size, mtime_ns, sha256),
        )

    def forget_hashes_except(self, keep: Iterable[str]) -> None:
        keep_set = set(keep)
        stale = [
            row[0]
            for row in self._db.execute("SELECT path FROM hashes")
            if row[0] not in keep_set
        ]
        self._db.executemany("DELETE FROM hashes WHERE path = ?", [(p,) for p in stale])
        self._db.commit()

    def commit(self) -> None:
        self._db.commit()

    # ----- base ------------------------------------------------------------------

    def base(self) -> dict[str, str]:
        return dict(self._db.execute("SELECT path, sha256 FROM base"))

    def set_base(self, base: Mapping[str, str]) -> None:
        with self._db:
            self._db.execute("DELETE FROM base")
            self._db.executemany(
                "INSERT INTO base (path, sha256) VALUES (?, ?)", base.items()
            )

    def clear(self) -> None:
        with self._db:
            self._db.execute("DELETE FROM hashes")
            self._db.execute("DELETE FROM base")
