"""On-demand bare-repository cache with LRU eviction.

Only projects that changed are cloned or updated; everything else is skipped.
The cache is capped at ``max_size`` bytes. Before a new repository is brought
in, the least recently used ones are evicted. A project larger than the whole
cache is processed in a temporary directory that is removed afterwards.

Cached repositories hold only ``refs/local/*`` (branches and tags mirrored from
the local GitLab) plus whatever refs the sync adds later; LFS objects are never
stored here.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from airtap_sync.gitsync.gitcmd import GitRunner

log = logging.getLogger(__name__)

INDEX_FILENAME = "index.sqlite"
TMP_DIRNAME = "tmp"

_SCHEMA = """
-- One row per cached bare repository.
CREATE TABLE IF NOT EXISTS repos (
    key TEXT PRIMARY KEY,        -- hash of the lowercased project path
    path TEXT NOT NULL,          -- project full_path, for humans
    size INTEGER NOT NULL,       -- bytes on disk after the last update
    last_used REAL NOT NULL      -- unix time of the last use
);
"""


def repo_key(full_path: str) -> str:
    """Directory name for a project: short hash, safe on every filesystem."""
    return hashlib.sha256(full_path.lower().encode("utf-8")).hexdigest()[:20]


def dir_size(path: Path) -> int:
    """Total size in bytes of all files below ``path``."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


class RepoCache:
    """LRU cache of bare repositories under ``root``."""

    def __init__(self, root: Path, max_size: int, runner: GitRunner) -> None:
        self.root = root
        self.max_size = max_size
        self.runner = runner
        self._pinned: set[str] = set()
        root.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(root / INDEX_FILENAME)
        self._db.executescript(_SCHEMA)
        self._reconcile()

    def close(self) -> None:
        self._db.close()

    # ----- index ---------------------------------------------------------------

    def repo_dir(self, full_path: str) -> Path:
        return self.root / f"{repo_key(full_path)}.git"

    def _reconcile(self) -> None:
        """Make the index and the directory contents agree after a crash."""
        shutil.rmtree(self.root / TMP_DIRNAME, ignore_errors=True)
        known = {row[0] for row in self._db.execute("SELECT key FROM repos")}
        on_disk = {p.name[:-4] for p in self.root.glob("*.git") if p.is_dir()}
        for key in known - on_disk:
            self._db.execute("DELETE FROM repos WHERE key = ?", (key,))
        for key in on_disk - known:
            log.warning("removing cache directory %s.git not in the index", key)
            shutil.rmtree(self.root / f"{key}.git", ignore_errors=True)
        self._db.commit()

    def contains(self, full_path: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM repos WHERE key = ?", (repo_key(full_path),)
        ).fetchone()
        return row is not None

    def total_size(self) -> int:
        return int(
            self._db.execute("SELECT COALESCE(SUM(size), 0) FROM repos").fetchone()[0]
        )

    def cached_paths(self) -> list[str]:
        return [
            row[0] for row in self._db.execute("SELECT path FROM repos ORDER BY path")
        ]

    # ----- eviction ------------------------------------------------------------

    def _evict_one(self) -> bool:
        """Remove the least recently used unpinned repository."""
        rows = self._db.execute(
            "SELECT key, path, size FROM repos ORDER BY last_used ASC"
        ).fetchall()
        for key, path, size in rows:
            if key in self._pinned:
                continue
            log.info("evicting %s (%d bytes) from the cache", path, size)
            shutil.rmtree(self.root / f"{key}.git", ignore_errors=True)
            self._db.execute("DELETE FROM repos WHERE key = ?", (key,))
            self._db.commit()
            return True
        return False

    def make_room(self, needed: int) -> bool:
        """Evict until ``needed`` more bytes fit; ``False`` if impossible."""
        while self.total_size() + needed > self.max_size:
            if not self._evict_one():
                return False
        return True

    # ----- use -----------------------------------------------------------------

    @contextlib.contextmanager
    def open(
        self, full_path: str, url: str, *, estimated_size: int = 0
    ) -> Iterator[Path]:
        """Yield a bare repo whose ``refs/local/*`` mirror the remote at ``url``.

        The repository is pinned for the duration of the block, so it cannot be
        evicted while in use. Its size and last-use time are recorded on exit.
        """
        if estimated_size > self.max_size:
            with self._open_temporary(full_path, url) as repo:
                yield repo
            return

        key = repo_key(full_path)
        repo = self.root / f"{key}.git"
        if not self.contains(full_path):
            self.make_room(estimated_size)
            shutil.rmtree(repo, ignore_errors=True)
        self._pinned.add(key)
        try:
            if not (repo / "HEAD").exists():
                self.runner.init_bare(repo)
            try:
                self.runner.fetch_local(repo, url)
            except BaseException:
                if not self.contains(full_path):
                    shutil.rmtree(repo, ignore_errors=True)
                raise
            self._record(key, full_path, dir_size(repo))
            yield repo
        finally:
            if repo.exists() and self.contains(full_path):
                self._record(key, full_path, dir_size(repo))
            self._pinned.discard(key)
            self.make_room(0)

    @contextlib.contextmanager
    def _open_temporary(self, full_path: str, url: str) -> Iterator[Path]:
        tmp = self.root / TMP_DIRNAME / repo_key(full_path)
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            self.runner.init_bare(tmp)
            self.runner.fetch_local(tmp, url)
            yield tmp
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _record(self, key: str, full_path: str, size: int) -> None:
        self._db.execute(
            "INSERT INTO repos (key, path, size, last_used) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET path = excluded.path, "
            "size = excluded.size, last_used = excluded.last_used",
            (key, full_path, size, time.time()),
        )
        self._db.commit()

    def remove(self, full_path: str) -> None:
        key = repo_key(full_path)
        shutil.rmtree(self.root / f"{key}.git", ignore_errors=True)
        self._db.execute("DELETE FROM repos WHERE key = ?", (key,))
        self._db.commit()

    def clear(self) -> None:
        """Drop every cached repository (used by ``git reset``)."""
        for (key,) in self._db.execute("SELECT key FROM repos").fetchall():
            shutil.rmtree(self.root / f"{key}.git", ignore_errors=True)
        self._db.execute("DELETE FROM repos")
        self._db.commit()
