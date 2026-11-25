"""Pre-populate the cache with recently active projects (``git warm``)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from airtap_sync.gitlab.projects import Project
from airtap_sync.gitsync.cache import RepoCache
from airtap_sync.gitsync.gitcmd import GitError

log = logging.getLogger(__name__)


@dataclass
class WarmResult:
    cloned: list[str] = field(default_factory=list)
    already_cached: int = 0
    no_room: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def select_candidates(
    projects: Iterable[Project], *, days: int, now: datetime | None = None
) -> list[Project]:
    """Non-empty projects active within ``days``, most recently active first."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    recent = [
        p
        for p in projects
        if p.last_activity_at
        and p.repository_size > 0
        and parse_time(p.last_activity_at) >= cutoff
    ]
    recent.sort(key=lambda p: parse_time(p.last_activity_at or ""), reverse=True)
    return recent


def warm_cache(
    cache: RepoCache,
    projects: Iterable[Project],
    clone_url: Callable[[str], str],
    *,
    days: int = 90,
    now: datetime | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> WarmResult:
    """Clone candidates into the cache without ever evicting anything.

    A project that would not fit in the remaining space is skipped, so warming
    fills the cache with the most recent projects and then stops growing it.
    """
    result = WarmResult()
    candidates = select_candidates(projects, days=days, now=now)
    for i, project in enumerate(candidates, start=1):
        if on_progress:
            on_progress(i, len(candidates), project.full_path)
        if cache.contains(project.full_path):
            result.already_cached += 1
            continue
        if cache.total_size() + project.repository_size > cache.max_size:
            result.no_room.append(project.full_path)
            continue
        try:
            with cache.open(
                project.full_path,
                clone_url(project.full_path),
                estimated_size=project.repository_size,
            ):
                pass
        except GitError as e:
            log.warning("warm: %s failed: %s", project.full_path, e)
            result.failed[project.full_path] = str(e)
        else:
            result.cloned.append(project.full_path)
    return result
