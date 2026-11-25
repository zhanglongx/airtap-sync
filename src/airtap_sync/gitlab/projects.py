"""Project discovery and the project operations needed for syncing."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from airtap_sync.gitlab.client import GitLabClient


@dataclass(frozen=True)
class Project:
    """The subset of a GitLab project that the sync cares about."""

    id: int
    full_path: str
    name: str
    description: str
    visibility: str
    default_branch: str | None
    archived: bool
    lfs_enabled: bool
    namespace_id: int
    namespace_kind: str  # "user" or "group"
    namespace_path: str
    http_url: str
    repository_size: int
    last_activity_at: str | None

    @property
    def key(self) -> str:
        """Case-insensitive identity used to match projects across both sides."""
        return self.full_path.lower()


def parse_project(raw: dict[str, Any]) -> Project:
    namespace = raw.get("namespace") or {}
    stats = raw.get("statistics") or {}
    return Project(
        id=int(raw["id"]),
        full_path=str(raw["path_with_namespace"]),
        name=str(raw.get("name") or raw["path"]),
        description=str(raw.get("description") or ""),
        visibility=str(raw.get("visibility") or "private"),
        default_branch=raw.get("default_branch"),
        archived=bool(raw.get("archived", False)),
        lfs_enabled=bool(raw.get("lfs_enabled", False)),
        namespace_id=int(namespace.get("id", 0)),
        namespace_kind=str(namespace.get("kind", "group")),
        namespace_path=str(namespace.get("full_path", "")),
        http_url=str(raw.get("http_url_to_repo", "")),
        repository_size=int(stats.get("repository_size") or 0),
        last_activity_at=raw.get("last_activity_at"),
    )


def list_projects(client: GitLabClient) -> list[Project]:
    """List every project visible to the token, with statistics.

    With an admin token this is every project on the instance.
    """
    params = {"statistics": "true", "order_by": "id", "sort": "asc"}
    return [parse_project(raw) for raw in client.paginate("/projects", params)]


def is_excluded(full_path: str, patterns: Iterable[str]) -> bool:
    """Whether ``full_path`` matches any exclude glob (case-insensitive).

    ``*`` also matches ``/``, so ``sandbox/*`` excludes nested groups too.
    """
    path = full_path.lower()
    return any(fnmatch.fnmatchcase(path, p.lower()) for p in patterns)


def filter_projects(
    projects: Iterable[Project], exclude_patterns: Iterable[str]
) -> list[Project]:
    patterns = tuple(exclude_patterns)
    return [p for p in projects if not is_excluded(p.full_path, patterns)]


@dataclass(frozen=True)
class CurrentUser:
    username: str
    is_admin: bool


def current_user(client: GitLabClient) -> CurrentUser:
    """The user the token belongs to (also verifies the token works)."""
    raw = client.get("/user")
    return CurrentUser(str(raw["username"]), bool(raw.get("is_admin", False)))
