from __future__ import annotations

import pytest

from airtap_sync.gitlab.client import (
    GitLabAuthError,
    GitLabClient,
    GitLabError,
    GitLabNotFound,
)
from tests.gitlab.fakegitlab import FakeGitLab


def make_client(fake: FakeGitLab, token: str = "secret", **kw) -> GitLabClient:
    sleeps: list[float] = []
    kw.setdefault("sleep", sleeps.append)
    client = GitLabClient(fake.url, token, **kw)
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client


def test_get_json(fake_gitlab: FakeGitLab) -> None:
    fake_gitlab.username = "bob"
    assert make_client(fake_gitlab).get("/user")["username"] == "bob"


def test_bad_token_is_auth_error(fake_gitlab: FakeGitLab) -> None:
    with pytest.raises(GitLabAuthError, match="令牌") as exc:
        make_client(fake_gitlab, "wrong").get("/user")
    assert exc.value.status == 401


def test_not_found(fake_gitlab: FakeGitLab) -> None:
    with pytest.raises(GitLabNotFound):
        make_client(fake_gitlab).get("/nothing")


def test_paginate_follows_pages(fake_gitlab: FakeGitLab) -> None:
    for i in range(1, 8):
        fake_gitlab.add_project(i, f"g/p{i}")
    items = list(make_client(fake_gitlab).paginate("/projects", per_page=3))
    assert [p["id"] for p in items] == list(range(1, 8))
    pages = [r for r in fake_gitlab.requests if "page=" in r[1]]
    assert len(pages) == 3


def test_paginate_empty(fake_gitlab: FakeGitLab) -> None:
    assert list(make_client(fake_gitlab).paginate("/projects")) == []


def test_get_retries_server_errors(fake_gitlab: FakeGitLab) -> None:
    fake_gitlab.fail_with = [503, 502]
    client = make_client(fake_gitlab, backoff=1.0)
    assert client.get("/user")["username"] == "syncbot"
    assert client.sleeps == [1.0, 2.0]  # type: ignore[attr-defined]


def test_get_gives_up_after_retries(fake_gitlab: FakeGitLab) -> None:
    fake_gitlab.fail_with = [503, 503, 503]
    with pytest.raises(GitLabError) as exc:
        make_client(fake_gitlab, retries=2).get("/user")
    assert exc.value.status == 503


def test_auth_errors_are_not_retried(fake_gitlab: FakeGitLab) -> None:
    fake_gitlab.fail_with = [403]
    client = make_client(fake_gitlab)
    with pytest.raises(GitLabAuthError):
        client.get("/user")
    assert client.sleeps == []  # type: ignore[attr-defined]
    assert len(fake_gitlab.requests) == 1


def test_connection_failure_message() -> None:
    client = GitLabClient("http://127.0.0.1:1", "t", retries=0, timeout=2)
    with pytest.raises(GitLabError, match="无法连接"):
        client.get("/user")
