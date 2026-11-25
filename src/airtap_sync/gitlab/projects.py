"""Project discovery and the project operations needed for syncing."""

from __future__ import annotations

import fnmatch
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from airtap_sync.gitlab.client import (
    GitLabAuthError,
    GitLabClient,
    GitLabError,
    GitLabNotFound,
)


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
    lfs_objects_size: int = 0
    # The token user's best role in the project (10 Guest … 40 Maintainer,
    # 50 Owner); ``None`` when the user is no member (the project is merely
    # visible, e.g. internal) or GitLab does not say.
    access_level: int | None = None

    def can_push(self, is_admin: bool = False) -> bool:
        """Whether the token may push. Admins can everywhere; others need a role."""
        if is_admin:
            return True
        return self.access_level is not None and self.access_level >= DEVELOPER

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
        lfs_objects_size=int(stats.get("lfs_objects_size") or 0),
        access_level=_access_level(raw.get("permissions")),
    )


DEVELOPER = 30
MAINTAINER = 40


def _access_level(permissions: Any) -> int | None:
    """Highest of the project and group access levels in ``permissions``."""
    if not isinstance(permissions, dict):
        return None
    levels = [
        int(entry["access_level"])
        for entry in (
            permissions.get("project_access"),
            permissions.get("group_access"),
        )
        if isinstance(entry, dict) and entry.get("access_level") is not None
    ]
    return max(levels) if levels else None


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


def in_scope(
    full_path: str, include: Iterable[str] = (), exclude: Iterable[str] = ()
) -> bool:
    """Whether a project takes part in the sync.

    With ``include`` patterns only matching projects do; ``exclude`` always wins.
    """
    include = tuple(include)
    if include and not is_excluded(full_path, include):
        return False
    return not is_excluded(full_path, exclude)


def filter_projects(
    projects: Iterable[Project],
    exclude_patterns: Iterable[str],
    include_patterns: Iterable[str] = (),
) -> list[Project]:
    include, exclude = tuple(include_patterns), tuple(exclude_patterns)
    return [p for p in projects if in_scope(p.full_path, include, exclude)]


@dataclass(frozen=True)
class CurrentUser:
    username: str
    is_admin: bool


def current_user(client: GitLabClient) -> CurrentUser:
    """The user the token belongs to (also verifies the token works)."""
    raw = client.get("/user")
    return CurrentUser(str(raw["username"]), bool(raw.get("is_admin", False)))


# ----- creating what the peer has --------------------------------------------


class UserNotFound(GitLabError):
    """A personal-namespace project needs a user the local GitLab lacks."""


def _quote(path: str) -> str:
    return urllib.parse.quote(path, safe="")


def find_group(client: GitLabClient, full_path: str) -> dict[str, Any] | None:
    try:
        return client.get(f"/groups/{_quote(full_path)}")
    except GitLabNotFound:
        return None


def ensure_group(
    client: GitLabClient, full_path: str, *, visibility: str = "private"
) -> int:
    """Return the id of the group at ``full_path``, creating missing levels."""
    parent_id: int | None = None
    walked: list[str] = []
    for segment in full_path.split("/"):
        walked.append(segment)
        current = "/".join(walked)
        group = find_group(client, current)
        if group is None:
            body: dict[str, Any] = {
                "name": segment,
                "path": segment,
                "visibility": visibility,
            }
            if parent_id is not None:
                body["parent_id"] = parent_id
            try:
                group = client.post("/groups", body)
            except GitLabError as e:
                raise GitLabError(
                    f"无法创建群组 {current}：{e}。没有管理员权限时通常不能创建群组，"
                    "请先让管理员在本端建好",
                    e.status,
                ) from None
        parent_id = int(group["id"])
    assert parent_id is not None
    return parent_id


def find_user_id(client: GitLabClient, username: str) -> int:
    users = client.get("/users", {"username": username}) or []
    for user in users:
        if str(user.get("username", "")).lower() == username.lower():
            return int(user["id"])
    raise UserNotFound(f"本端 GitLab 上没有用户 {username}，请先创建该用户")


def create_project(
    client: GitLabClient,
    *,
    full_path: str,
    name: str,
    description: str,
    visibility: str,
    namespace_kind: str,
    namespace_path: str,
    lfs_enabled: bool,
    own_username: str | None = None,
) -> Project:
    """Create an empty project at ``full_path`` (groups are created as needed).

    ``own_username`` is the token's user: a project in that user's own personal
    namespace can be created by anyone, whereas one in somebody else's personal
    namespace needs an admin.
    """
    path = full_path.rsplit("/", 1)[-1]
    body: dict[str, Any] = {
        "name": name,
        "path": path,
        "description": description,
        "visibility": visibility,
        "lfs_enabled": lfs_enabled,
        "initialize_with_readme": False,
    }
    if namespace_kind == "user" and own_username == namespace_path:
        raw = client.post("/projects", body)  # lands in the caller's namespace
    elif namespace_kind == "user":
        user_id = find_user_id(client, namespace_path)
        try:
            raw = client.post(f"/projects/user/{user_id}", body)
        except GitLabAuthError:
            raise GitLabAuthError(
                f"无法在用户 {namespace_path} 的个人命名空间下建工程：需要管理员权限",
                403,
            ) from None
    else:
        body["namespace_id"] = ensure_group(
            client, namespace_path, visibility=visibility
        )
        try:
            raw = client.post("/projects", body)
        except GitLabError as e:
            # A project may not be more visible than its group; fall back.
            if "visibility" not in str(e).lower() or visibility == "private":
                raise
            body["visibility"] = "private"
            raw = client.post("/projects", body)
    return parse_project(raw)


def set_default_branch(client: GitLabClient, project_id: int, branch: str) -> None:
    client.put(f"/projects/{project_id}", {"default_branch": branch})


def archive_project(client: GitLabClient, project_id: int) -> None:
    client.post(f"/projects/{project_id}/archive")
