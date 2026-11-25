from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from airtap_sync.gitlab.projects import Project, parse_project
from airtap_sync.gitsync.cache import RepoCache
from airtap_sync.gitsync.gitcmd import GitRunner
from airtap_sync.gitsync.warm import select_candidates, warm_cache
from tests.gitsync.gitrepo import Remote

NOW = datetime(2026, 11, 25, tzinfo=UTC)


def project(
    path: str, *, size: int = 1000, active: str | None = "2026-11-20"
) -> Project:
    raw = {
        "id": abs(hash(path)) % 10**6,
        "path": path.rsplit("/", 1)[-1],
        "path_with_namespace": path,
        "statistics": {"repository_size": size},
        "last_activity_at": f"{active}T08:00:00.000Z" if active else None,
    }
    return parse_project(raw)


def test_select_candidates() -> None:
    projects = [
        project("g/old", active="2025-01-01"),
        project("g/empty", size=0),
        project("g/never", active=None),
        project("g/recent", active="2026-11-24"),
        project("g/mid", active="2026-10-01"),
    ]
    picked = select_candidates(projects, days=90, now=NOW)
    assert [p.full_path for p in picked] == ["g/recent", "g/mid"]


def test_warm_cache(tmp_path: Path) -> None:
    remotes = {}
    for name in ("a", "b", "c"):
        r = Remote(tmp_path, name)
        r.commit("f", name)
        remotes[f"g/{name}"] = r
    projects = [
        project("g/a", active="2026-11-24"),
        project("g/b", active="2026-11-23"),
        project("g/c", active="2026-11-22"),
        project("g/missing", active="2026-11-21"),
    ]
    urls = {**{k: v.url for k, v in remotes.items()}}
    urls["g/missing"] = (tmp_path / "missing.git").as_uri()

    cache = RepoCache(tmp_path / "cache", 10**9, GitRunner())
    with cache.open("g/a", urls["g/a"]):
        pass
    progress: list[tuple[int, int, str]] = []
    result = warm_cache(
        cache,
        projects,
        urls.__getitem__,
        days=90,
        now=NOW,
        on_progress=lambda i, n, p: progress.append((i, n, p)),
    )
    assert result.already_cached == 1
    assert result.cloned == ["g/b", "g/c"]
    assert list(result.failed) == ["g/missing"]
    assert result.no_room == []
    assert progress[-1] == (4, 4, "g/missing")
    assert cache.cached_paths() == ["g/a", "g/b", "g/c"]


def test_warm_never_evicts(tmp_path: Path) -> None:
    pre, other = Remote(tmp_path, "pre"), Remote(tmp_path, "other")
    pre.commit("f", "x" * 1000)
    other.commit("f", "y" * 1000)
    cache = RepoCache(tmp_path / "cache", 10**9, GitRunner())
    with cache.open("g/pre", pre.url):
        pass
    cache.max_size = cache.total_size() + 100  # no room for anything else

    result = warm_cache(
        cache,
        [project("g/other", size=10_000, active="2026-11-24")],
        lambda _path: other.url,
        now=NOW,
    )
    assert result.no_room == ["g/other"]
    assert result.cloned == []
    assert cache.cached_paths() == ["g/pre"]
