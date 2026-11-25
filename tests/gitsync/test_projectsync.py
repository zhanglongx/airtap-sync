from __future__ import annotations

from pathlib import Path

import pytest

from airtap_sync.config import GitLabCacheConfig, GitLabConfig
from airtap_sync.gitlab.client import GitLabClient
from airtap_sync.gitsync.cache import RepoCache
from airtap_sync.gitsync.context import GitContext
from airtap_sync.gitsync.gitcmd import GitRunner
from airtap_sync.gitsync.manifest import ProjectRecord
from airtap_sync.gitsync.projectsync import sync_project
from airtap_sync.gitsync.refsync import export_bundle, read_local_refs
from airtap_sync.gitsync.state import GitState
from tests.gitlab.fakegitlab import FakeGitLab
from tests.gitsync.gitrepo import Remote

H = "refs/heads/"
T = "refs/tags/"
STAMP = "20260101T000000Z"


@pytest.fixture
def source(tmp_path: Path) -> Remote:
    remote = Remote(tmp_path, "source")
    remote.commit("f", "1")
    remote.commit("g", "2", branch="dev")
    remote.tag("v1")
    return remote


def make_bundle(tmp_path: Path, source: Remote) -> tuple[Path, dict[str, str]]:
    runner = GitRunner()
    cache = RepoCache(tmp_path / "cacheA", 10**9, runner)
    out = tmp_path / "full.bundle"
    with cache.open("team/app", source.url) as repo:
        refs = read_local_refs(runner, repo)
        assert export_bundle(runner, repo, out, refs, peer_refs={}, base={}, full=True)
    cache.close()
    return out, refs


def make_ctx(tmp_path: Path, fake: FakeGitLab, remotes: dict[str, Remote]):
    runner = GitRunner()
    ctx = GitContext(
        config=GitLabConfig(
            url=fake.url, cache=GitLabCacheConfig(dir=tmp_path / "cacheB")
        ),
        client=GitLabClient(fake.url, "secret"),
        runner=runner,
        cache=RepoCache(tmp_path / "cacheB", 10**9, runner),
        state=GitState(tmp_path / "state.sqlite"),
    )
    ctx.clone_url = lambda path: remotes[path].url  # type: ignore[method-assign]
    return ctx


def record(refs: dict[str, str], **kw) -> ProjectRecord:
    base = {
        "path": "team/app",
        "project_id": 1,
        "name": "App",
        "description": "the app",
        "visibility": "internal",
        "default_branch": "dev",
        "archived": False,
        "lfs_enabled": True,
        "namespace_kind": "group",
        "namespace_path": "team",
        "refs": refs,
    }
    return ProjectRecord(**{**base, **kw})


def test_create_project_from_bundle(
    tmp_path: Path, fake_gitlab: FakeGitLab, source: Remote
) -> None:
    bundle, refs = make_bundle(tmp_path, source)
    target = Remote(tmp_path, "target")  # the repo GitLab B "creates"
    ctx = make_ctx(tmp_path, fake_gitlab, {"team/app": target})

    result = sync_project(
        ctx,
        record(refs, archived=True),
        "team/app",
        peer_side="A",
        bundle=bundle,
        create=True,
        trash_stamp=STAMP,
    )

    assert result.error is None and result.created
    assert result.notes == []
    created = next(
        p for p in fake_gitlab.projects if p["path_with_namespace"] == "team/app"
    )
    assert (created["name"], created["visibility"]) == ("App", "internal")
    assert created["description"] == "the app"
    assert created["default_branch"] == "dev"
    assert created["archived"] is True
    assert target.rev("main") == source.rev("main")
    assert target.rev("dev") == source.rev("dev")
    assert target.rev("v1") == source.rev("v1")
    assert ctx.state.base_projects() == {"team/app": "team/app"}
    assert ctx.state.base_refs("team/app") == refs
    assert ctx.state.peer_refs("team/app") == refs
    assert result.local_refs == refs


def test_missing_user_is_recorded_not_raised(
    tmp_path: Path, fake_gitlab: FakeGitLab, source: Remote
) -> None:
    bundle, refs = make_bundle(tmp_path, source)
    ctx = make_ctx(tmp_path, fake_gitlab, {})
    peer = record(
        refs,
        path="alice/notes",
        namespace_kind="user",
        namespace_path="alice",
    )
    result = sync_project(
        ctx,
        peer,
        "alice/notes",
        peer_side="A",
        bundle=bundle,
        create=True,
        trash_stamp=STAMP,
    )
    assert result.error and "alice" in result.error
    assert result.local_refs is None
    assert ctx.state.base_projects() == {}


def test_default_branch_not_synced_is_noted(
    tmp_path: Path, fake_gitlab: FakeGitLab, source: Remote
) -> None:
    bundle, refs = make_bundle(tmp_path, source)
    target = Remote(tmp_path, "target")
    ctx = make_ctx(tmp_path, fake_gitlab, {"team/app": target})
    result = sync_project(
        ctx,
        record(refs, default_branch="gone"),
        "team/app",
        peer_side="A",
        bundle=bundle,
        create=True,
        trash_stamp=STAMP,
    )
    assert result.error is None
    assert any("gone" in n for n in result.notes)


def test_existing_project_is_merged_incrementally(
    tmp_path: Path, fake_gitlab: FakeGitLab, source: Remote
) -> None:
    bundle, refs = make_bundle(tmp_path, source)
    target = Remote(tmp_path, "target")
    ctx = make_ctx(tmp_path, fake_gitlab, {"team/app": target})
    peer = record(refs)
    sync_project(
        ctx,
        peer,
        "team/app",
        peer_side="A",
        bundle=bundle,
        create=True,
        trash_stamp=STAMP,
    )

    # The peer moves on; the second run is an ordinary (non-creating) sync.
    source.commit("h", "3")
    runner = GitRunner()
    cache = RepoCache(tmp_path / "cacheA2", 10**9, runner)
    with cache.open("team/app", source.url) as repo:
        new_refs = read_local_refs(runner, repo)
        inc = tmp_path / "inc.bundle"
        assert export_bundle(
            runner,
            repo,
            inc,
            new_refs,
            peer_refs=ctx.state.peer_refs("team/app"),
            base={},
        )
    result = sync_project(
        ctx,
        record(new_refs),
        "team/app",
        peer_side="A",
        bundle=inc,
        create=False,
        trash_stamp=STAMP,
    )
    assert result.error is None and not result.created
    assert target.rev("main") == source.rev("main")
    assert ctx.state.base_refs("team/app") == new_refs


def test_unreadable_peer_refs_are_reported(
    tmp_path: Path, fake_gitlab: FakeGitLab
) -> None:
    ctx = make_ctx(tmp_path, fake_gitlab, {})
    peer = ProjectRecord(**{**record({}).to_dict(), "refs": None})
    result = sync_project(
        ctx,
        peer,
        "team/app",
        peer_side="A",
        bundle=None,
        create=False,
        trash_stamp=STAMP,
    )
    assert result.error and "分支列表" in result.error
