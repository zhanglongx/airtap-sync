from __future__ import annotations

import pytest

from airtap_sync.gitlab.client import GitLabClient, GitLabError
from airtap_sync.gitlab.projects import (
    UserNotFound,
    archive_project,
    create_project,
    ensure_group,
    find_user_id,
    set_default_branch,
)
from tests.gitlab.fakegitlab import FakeGitLab


@pytest.fixture
def client(fake_gitlab: FakeGitLab) -> GitLabClient:
    return GitLabClient(fake_gitlab.url, "secret")


def test_ensure_group_creates_missing_levels(
    client: GitLabClient, fake_gitlab: FakeGitLab
) -> None:
    gid = ensure_group(client, "org/team/sub")
    assert set(fake_gitlab.groups) == {"org", "org/team", "org/team/sub"}
    assert fake_gitlab.groups["org/team/sub"]["id"] == gid
    assert (
        fake_gitlab.groups["org/team/sub"]["parent_id"]
        == (fake_gitlab.groups["org/team"]["id"])
    )
    posts = [r for r in fake_gitlab.requests if r[0] == "POST"]
    assert ensure_group(client, "org/team/sub") == gid  # idempotent
    assert [r for r in fake_gitlab.requests if r[0] == "POST"] == posts


def test_ensure_group_reuses_existing(
    client: GitLabClient, fake_gitlab: FakeGitLab
) -> None:
    existing = fake_gitlab.add_group("org")["id"]
    ensure_group(client, "org/new")
    assert fake_gitlab.groups["org/new"]["parent_id"] == existing
    assert len([b for b in fake_gitlab.bodies if b[0] == "/api/v4/groups"]) == 1


def test_create_project_in_group(client: GitLabClient, fake_gitlab: FakeGitLab) -> None:
    project = create_project(
        client,
        full_path="org/team/App",
        name="The App",
        description="desc",
        visibility="internal",
        namespace_kind="group",
        namespace_path="org/team",
        lfs_enabled=True,
    )
    assert project.full_path == "org/team/App"
    assert project.name == "The App"
    assert project.visibility == "internal"
    assert project.lfs_enabled
    assert "org/team" in fake_gitlab.groups
    body = fake_gitlab.bodies[-1][1]
    assert body["path"] == "App" and body["initialize_with_readme"] is False


def test_create_project_in_user_namespace(
    client: GitLabClient, fake_gitlab: FakeGitLab
) -> None:
    fake_gitlab.users["alice"] = 7
    project = create_project(
        client,
        full_path="alice/notes",
        name="notes",
        description="",
        visibility="private",
        namespace_kind="user",
        namespace_path="alice",
        lfs_enabled=False,
    )
    assert project.full_path == "alice/notes"
    assert project.namespace_kind == "user"
    assert fake_gitlab.bodies[-1][0] == "/api/v4/projects/user/7"


def test_missing_user_is_reported(client: GitLabClient) -> None:
    with pytest.raises(UserNotFound, match="bob"):
        find_user_id(client, "bob")


def test_public_project_in_private_group_falls_back_to_private(
    client: GitLabClient, fake_gitlab: FakeGitLab
) -> None:
    fake_gitlab.private_groups = True
    project = create_project(
        client,
        full_path="org/App",
        name="App",
        description="",
        visibility="public",
        namespace_kind="group",
        namespace_path="org",
        lfs_enabled=False,
    )
    assert project.visibility == "private"


def test_duplicate_project_raises(
    client: GitLabClient, fake_gitlab: FakeGitLab
) -> None:
    kwargs = {
        "full_path": "org/App",
        "name": "App",
        "description": "",
        "visibility": "private",
        "namespace_kind": "group",
        "namespace_path": "org",
        "lfs_enabled": False,
    }
    create_project(client, **kwargs)
    with pytest.raises(GitLabError, match="taken"):
        create_project(client, **kwargs)


def test_default_branch_and_archive(
    client: GitLabClient, fake_gitlab: FakeGitLab
) -> None:
    raw = fake_gitlab.add_project(5, "org/App")
    set_default_branch(client, 5, "develop")
    archive_project(client, 5)
    assert raw["default_branch"] == "develop"
    assert raw["archived"] is True


def test_non_admin_cannot_create_in_user_namespace_or_top_level_group(
    client: GitLabClient, fake_gitlab: FakeGitLab
) -> None:
    from airtap_sync.gitlab.client import GitLabAuthError

    fake_gitlab.is_admin = False
    fake_gitlab.users["alice"] = 7
    kwargs = {
        "name": "n",
        "description": "",
        "visibility": "private",
        "lfs_enabled": False,
    }
    with pytest.raises(GitLabAuthError, match="需要管理员权限"):
        create_project(
            client,
            full_path="alice/notes",
            namespace_kind="user",
            namespace_path="alice",
            **kwargs,
        )
    with pytest.raises(GitLabError, match="无法创建群组 neworg"):
        create_project(
            client,
            full_path="neworg/app",
            namespace_kind="group",
            namespace_path="neworg",
            **kwargs,
        )
    # Subgroups of an existing group are fine for a non-admin.
    fake_gitlab.add_group("team")
    project = create_project(
        client,
        full_path="team/sub/app",
        namespace_kind="group",
        namespace_path="team/sub",
        **kwargs,
    )
    assert project.full_path == "team/sub/app"


def test_project_in_own_personal_namespace_needs_no_admin(
    client: GitLabClient, fake_gitlab: FakeGitLab
) -> None:
    fake_gitlab.is_admin = False
    fake_gitlab.username = "zhanglx"
    project = create_project(
        client,
        full_path="zhanglx/notes",
        name="notes",
        description="",
        visibility="private",
        namespace_kind="user",
        namespace_path="zhanglx",
        lfs_enabled=False,
        own_username="zhanglx",
    )
    assert project.full_path == "zhanglx/notes"
    assert fake_gitlab.bodies[-1][0] == "/api/v4/projects"
