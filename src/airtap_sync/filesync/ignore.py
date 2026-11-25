"""Gitignore-style ignore rules for synchronised folders."""

from __future__ import annotations

from collections.abc import Iterable

import pathspec

# Never worth synchronising: VCS data, editor/OS litter, our own bookkeeping.
DEFAULT_PATTERNS: tuple[str, ...] = (
    ".git/",
    "~$*",
    "*.tmp",
    "Thumbs.db",
    "desktop.ini",
    ".DS_Store",
    ".airtap-sync/",
)
INTERNAL_DIR = ".airtap-sync"


class IgnoreRules:
    """The default patterns plus the folder's own; ``a/b/`` matches directories."""

    def __init__(self, patterns: Iterable[str] = ()) -> None:
        self.extra: tuple[str, ...] = tuple(p for p in patterns if p.strip())
        self._spec = pathspec.GitIgnoreSpec.from_lines([*DEFAULT_PATTERNS, *self.extra])

    def ignores(self, rel: str, *, is_dir: bool = False) -> bool:
        """Whether ``rel`` (``/``-separated, relative to the root) is ignored."""
        if rel == INTERNAL_DIR or rel.startswith(INTERNAL_DIR + "/"):
            return True
        return self._spec.match_file(rel + "/" if is_dir else rel)

    def union(self, other: Iterable[str]) -> IgnoreRules:
        """Rules ignoring whatever either side's patterns ignore."""
        return IgnoreRules([*self.extra, *other])
