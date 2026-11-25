"""Persistent Git sync state (sqlite): agreed base, peer view, project ids."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path

from airtap_sync.gitsync.gitcmd import RefMap

_SCHEMA = """
-- Projects both sides were seen to have; the project-level base.
CREATE TABLE IF NOT EXISTS base_projects (
    key TEXT PRIMARY KEY,         -- lowercased full path
    path TEXT NOT NULL            -- full path as written on this side
);

-- Refs both sides were seen to agree on, per project; the ref-level base.
CREATE TABLE IF NOT EXISTS base_refs (
    key TEXT NOT NULL,
    ref TEXT NOT NULL,
    oid TEXT NOT NULL,
    PRIMARY KEY (key, ref)
);

-- Refs the peer reported in its last imported manifest (what it is known to have).
CREATE TABLE IF NOT EXISTS peer_refs (
    key TEXT NOT NULL,
    ref TEXT NOT NULL,
    oid TEXT NOT NULL,
    PRIMARY KEY (key, ref)
);

-- Local GitLab project ids seen on the last run, to recognise renames.
CREATE TABLE IF NOT EXISTS local_ids (
    project_id INTEGER PRIMARY KEY,
    path TEXT NOT NULL
);
"""


class GitState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ----- project-level base ----------------------------------------------------

    def base_projects(self) -> dict[str, str]:
        """``{key: path}`` of projects both sides had at the last agreement."""
        return dict(self._db.execute("SELECT key, path FROM base_projects"))

    def set_base_projects(self, projects: Mapping[str, str]) -> None:
        with self._db:
            self._db.execute("DELETE FROM base_projects")
            self._db.executemany(
                "INSERT INTO base_projects (key, path) VALUES (?, ?)",
                projects.items(),
            )
            # Refs of projects that left the base are meaningless now.
            self._db.execute(
                "DELETE FROM base_refs WHERE key NOT IN (SELECT key FROM base_projects)"
            )

    def add_base_project(self, key: str, path: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO base_projects (key, path) VALUES (?, ?)",
                (key, path),
            )

    # ----- ref-level base and peer view ---------------------------------------------

    def _get(self, table: str, key: str) -> RefMap:
        rows = self._db.execute(f"SELECT ref, oid FROM {table} WHERE key = ?", (key,))
        return dict(rows)

    def _put(self, table: str, key: str, refs: Mapping[str, str]) -> None:
        with self._db:
            self._db.execute(f"DELETE FROM {table} WHERE key = ?", (key,))
            self._db.executemany(
                f"INSERT INTO {table} (key, ref, oid) VALUES (?, ?, ?)",
                [(key, ref, oid) for ref, oid in refs.items()],
            )

    def base_refs(self, key: str) -> RefMap:
        return self._get("base_refs", key)

    def set_base_refs(self, key: str, refs: Mapping[str, str]) -> None:
        self._put("base_refs", key, refs)

    def peer_refs(self, key: str) -> RefMap:
        return self._get("peer_refs", key)

    def set_peer_refs(self, key: str, refs: Mapping[str, str]) -> None:
        self._put("peer_refs", key, refs)

    def forget_project(self, key: str) -> None:
        with self._db:
            for table in ("base_projects", "base_refs", "peer_refs"):
                self._db.execute(f"DELETE FROM {table} WHERE key = ?", (key,))

    # ----- local ids ---------------------------------------------------------------

    def local_ids(self) -> dict[int, str]:
        return dict(self._db.execute("SELECT project_id, path FROM local_ids"))

    def set_local_ids(self, ids: Mapping[int, str]) -> None:
        with self._db:
            self._db.execute("DELETE FROM local_ids")
            self._db.executemany(
                "INSERT INTO local_ids (project_id, path) VALUES (?, ?)", ids.items()
            )

    # ----- bulk ----------------------------------------------------------------------

    def replace_base(
        self, projects: Mapping[str, str], refs: Mapping[str, Mapping[str, str]]
    ) -> None:
        """Replace the whole base (used when adopting a baseline snapshot)."""
        self.clear()
        self.set_base_projects(projects)
        for key, project_refs in refs.items():
            if key in projects:
                self.set_base_refs(key, project_refs)

    def clear(self) -> None:
        with self._db:
            for table in ("base_projects", "base_refs", "peer_refs", "local_ids"):
                self._db.execute(f"DELETE FROM {table}")

    def known_keys(self) -> Iterable[str]:
        return [row[0] for row in self._db.execute("SELECT key FROM base_projects")]
