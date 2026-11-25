from __future__ import annotations

import pytest

from airtap_sync.gitlab.client import GitLabClient
from airtap_sync.gitlab.projects import (
    current_user,
    filter_projects,
    in_scope,
    is_excluded,
    list_projects,
    parse_project,
)
from tests.gitlab.fakegitlab import FakeGitLab


def test_list_projects_parses_fields(fake_gitlab: FakeGitLab) -> None:
    fake_gitlab.add_project(
        7,
        "team/sub/App",
        archived=True,
        default_branch=None,
        statistics={"repository_size": 4096},
    )
    fake_gitlab.add_project(
        9,
        "alice/notes",
        namespace={"id": 5, "kind": "user", "full_path": "alice"},
    )
    projects = list_projects(GitLabClient(fake_gitlab.url, "secret"))

    app, notes = projects
    assert app.id == 7
    assert app.full_path == "team/sub/App"
    assert app.key == "team/sub/app"
    assert app.archived is True
    assert app.default_branch is None
    assert app.repository_size == 4096
    assert app.namespace_path == "team/sub"
    assert notes.namespace_kind == "user"
    assert notes.namespace_path == "alice"
    assert "statistics=true" in fake_gitlab.requests[0][1]


def test_list_projects_many_pages(fake_gitlab: FakeGitLab) -> None:
    for i in range(1, 251):
        fake_gitlab.add_project(i, f"g{i % 5}/p{i}")
    projects = list_projects(GitLabClient(fake_gitlab.url, "secret"))
    assert len(projects) == 250
    assert [p.id for p in projects] == list(range(1, 251))


def test_parse_project_tolerates_missing_optional_fields() -> None:
    project = parse_project(
        {"id": 1, "path": "x", "path_with_namespace": "g/x", "description": None}
    )
    assert project.description == ""
    assert project.repository_size == 0
    assert project.visibility == "private"
    assert project.last_activity_at is None


@pytest.mark.parametrize(
    ("path", "patterns", "expected"),
    [
        ("sandbox/a", ["sandbox/*"], True),
        ("sandbox/x/y", ["sandbox/*"], True),
        ("Sandbox/a", ["sandbox/*"], True),
        ("team/sandbox", ["sandbox/*"], False),
        ("team/a", [], False),
        ("team/tmp-1", ["*/tmp-*"], True),
    ],
)
def test_is_excluded(path: str, patterns: list[str], expected: bool) -> None:
    assert is_excluded(path, patterns) is expected


def test_filter_projects(fake_gitlab: FakeGitLab) -> None:
    fake_gitlab.add_project(1, "sandbox/a")
    fake_gitlab.add_project(2, "team/b")
    projects = list_projects(GitLabClient(fake_gitlab.url, "secret"))
    kept = filter_projects(projects, ["sandbox/*"])
    assert [p.full_path for p in kept] == ["team/b"]


def test_current_user(fake_gitlab: FakeGitLab) -> None:
    fake_gitlab.is_admin = False
    user = current_user(GitLabClient(fake_gitlab.url, "secret"))
    assert (user.username, user.is_admin) == ("syncbot", False)


@pytest.mark.parametrize(
    ("path", "include", "exclude", "expected"),
    [
        ("zh/app", ["zh/*"], [], True),
        ("zh/sub/app", ["zh/*"], [], True),
        ("other/app", ["zh/*"], [], False),
        ("ZH/App", ["zh/*"], [], True),
        ("zh/tmp/x", ["zh/*"], ["zh/tmp/*"], False),  # exclude wins
        ("any/thing", [], [], True),
        ("any/thing", [], ["any/*"], False),
    ],
)
def test_in_scope(path: str, include: list, exclude: list, expected: bool) -> None:
    assert in_scope(path, include, exclude) is expected


def test_filter_projects_with_include(fake_gitlab: FakeGitLab) -> None:
    for i, path in enumerate(["zh/a", "zh/b", "other/c"], start=1):
        fake_gitlab.add_project(i, path)
    projects = list_projects(GitLabClient(fake_gitlab.url, "secret"))
    kept = filter_projects(projects, [], ["zh/*"])
    assert [p.full_path for p in kept] == ["zh/a", "zh/b"]


def test_access_level_is_the_best_of_project_and_group_roles() -> None:
    def project(perms: object):
        return parse_project(
            {
                "id": 1,
                "path": "x",
                "path_with_namespace": "g/x",
                "permissions": perms,
            }
        )

    both = project(
        {"project_access": {"access_level": 20}, "group_access": {"access_level": 50}}
    )
    assert both.access_level == 50
    assert both.can_push()
    assert project({"project_access": None, "group_access": None}).access_level is None
    assert project(None).access_level is None
    assert not project(None).can_push()
    assert project(None).can_push(is_admin=True)
    assert not project({"project_access": {"access_level": 20}}).can_push()


def test_writable_is_resolved_by_the_server_including_inherited_roles(
    fake_gitlab: FakeGitLab,
) -> None:
    fake_gitlab.add_project(1, "ZH/tetris-web/app")
    fake_gitlab.add_project(2, "ZH/other")
    fake_gitlab.projects[0]["inherited_level"] = 50  # list shows permissions: null
    projects = list_projects(GitLabClient(fake_gitlab.url, "secret"))
    first, second = projects
    assert first.access_level is None  # what the list itself says
    assert first.writable is True  # what min_access_level says
    assert first.can_push()
    assert second.writable is False and not second.can_push()
    assert second.can_push(is_admin=True)
    unresolved = list_projects(
        GitLabClient(fake_gitlab.url, "secret"), resolve_access=False
    )
    assert unresolved[0].writable is None
