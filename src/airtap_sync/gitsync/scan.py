"""Change detection: read every project's branches and tags over the network."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from airtap_sync.gitsync.gitcmd import GitError, GitRunner, RefMap

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteRefs:
    """Result of ``git ls-remote`` for one project."""

    key: str
    refs: RefMap | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def ls_remote_all(
    runner: GitRunner,
    targets: Iterable[tuple[str, str]],
    *,
    concurrency: int = 8,
    on_done: Callable[[RemoteRefs], None] | None = None,
) -> dict[str, RemoteRefs]:
    """Run ``git ls-remote`` for ``(key, url)`` pairs in parallel.

    A failing project does not abort the rest; its error is recorded instead.
    ``on_done`` is called from worker threads as each project finishes.
    """

    def one(target: tuple[str, str]) -> RemoteRefs:
        key, url = target
        try:
            result = RemoteRefs(key, refs=runner.ls_remote(url))
        except GitError as e:
            log.warning("ls-remote failed for %s: %s", key, e)
            result = RemoteRefs(key, error=str(e))
        if on_done is not None:
            on_done(result)
        return result

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        return {r.key: r for r in pool.map(one, targets)}
