from __future__ import annotations

from pathlib import Path

import pytest

from airtap_sync.gitsync.cache import RepoCache, repo_key
from airtap_sync.gitsync.gitcmd import GitError, GitRunner
from tests.gitsync.gitrepo import Remote

BIG = 10**9


def make_cache(tmp_path: Path, max_size: int = BIG) -> RepoCache:
    return RepoCache(tmp_path / "cache", max_size, GitRunner())


def test_first_open_clones_refs_local(tmp_path: Path) -> None:
    remote = Remote(tmp_path, "r")
    sha = remote.commit("a", "1")
    remote.tag("v1")
    cache = make_cache(tmp_path)
    with cache.open("Team/App", remote.url) as repo:
        refs = GitRunner().list_refs(repo, "refs/local")
        assert refs == {
            "refs/local/heads/main": sha,
            "refs/local/tags/v1": sha,
        }
    assert cache.contains("team/app")  # case-insensitive
    assert cache.total_size() > 0
    assert cache.cached_paths() == ["Team/App"]


def test_second_open_fetches_increment_and_prunes(tmp_path: Path) -> None:
    remote = Remote(tmp_path, "r")
    remote.commit("a", "1")
    remote.commit("b", "x", branch="old")
    cache = make_cache(tmp_path)
    with cache.open("g/r", remote.url):
        pass
    new = remote.commit("a", "2")
    remote.delete_branch("old")
    with cache.open("g/r", remote.url) as repo:
        refs = GitRunner().list_refs(repo, "refs/local/heads")
    assert refs == {"refs/local/heads/main": new}


def test_empty_remote(tmp_path: Path) -> None:
    remote = Remote(tmp_path, "r")
    cache = make_cache(tmp_path)
    with cache.open("g/r", remote.url) as repo:
        assert GitRunner().list_refs(repo, "refs/local") == {}


def test_failed_clone_leaves_nothing(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    with pytest.raises(GitError), cache.open("g/x", (tmp_path / "no.git").as_uri()):
        pass
    assert not cache.contains("g/x")
    assert not cache.repo_dir("g/x").exists()


def test_lru_eviction(tmp_path: Path) -> None:
    remotes = []
    for name in "abc":
        r = Remote(tmp_path, name)
        r.commit("f", name * 1000)
        remotes.append(r)
    probe = make_cache(tmp_path)
    with probe.open("g/probe", remotes[0].url):
        pass
    one = probe.total_size()
    probe.close()
    cache = RepoCache(tmp_path / "cache2", int(one * 2.5), GitRunner())

    for name, r in zip("abc", remotes, strict=True):
        with cache.open(f"g/{name}", r.url):
            pass
    # Capacity is ~2.5 repos: the oldest (a) is evicted by c.
    assert not cache.contains("g/a")
    assert cache.contains("g/b")
    assert cache.contains("g/c")

    with cache.open("g/b", remotes[1].url):  # touch b -> c becomes oldest
        pass
    with cache.open("g/a", remotes[0].url):
        pass
    assert cache.contains("g/b")
    assert cache.contains("g/a")
    assert not cache.contains("g/c")
    assert cache.total_size() <= cache.max_size


def test_pinned_repo_is_not_evicted(tmp_path: Path) -> None:
    a, b = Remote(tmp_path, "a"), Remote(tmp_path, "b")
    a.commit("f", "x" * 1000)
    b.commit("f", "y" * 1000)
    cache = make_cache(tmp_path, max_size=1)  # everything is "too big"
    with cache.open("g/a", a.url, estimated_size=0) as repo_a:
        with cache.open("g/b", b.url, estimated_size=0):
            assert repo_a.exists()
            assert cache.contains("g/a")
        assert repo_a.exists()


def test_oversized_project_uses_temporary_dir(tmp_path: Path) -> None:
    remote = Remote(tmp_path, "r")
    sha = remote.commit("a", "1")
    cache = make_cache(tmp_path, max_size=1000)
    with cache.open("g/huge", remote.url, estimated_size=5000) as repo:
        assert GitRunner().list_refs(repo, "refs/local/heads") == {
            "refs/local/heads/main": sha
        }
        assert "tmp" in repo.parts
    assert not repo.exists()
    assert not cache.contains("g/huge")


def test_reconcile_after_crash(tmp_path: Path) -> None:
    remote = Remote(tmp_path, "r")
    remote.commit("a", "1")
    cache = make_cache(tmp_path)
    with cache.open("g/r", remote.url):
        pass
    cache.close()

    root = tmp_path / "cache"
    orphan = root / "deadbeefdeadbeef0000.git"
    orphan.mkdir()
    (root / "tmp" / "x").mkdir(parents=True)
    import shutil

    shutil.rmtree(root / f"{repo_key('g/r')}.git")  # index row without directory

    cache = RepoCache(root, BIG, GitRunner())
    assert not orphan.exists()
    assert not (root / "tmp").exists()
    assert not cache.contains("g/r")


def test_remove_and_clear(tmp_path: Path) -> None:
    remote = Remote(tmp_path, "r")
    remote.commit("a", "1")
    cache = make_cache(tmp_path)
    for path in ("g/one", "g/two"):
        with cache.open(path, remote.url):
            pass
    cache.remove("g/one")
    assert cache.cached_paths() == ["g/two"]
    cache.clear()
    assert cache.cached_paths() == []
    assert cache.total_size() == 0
    assert not list((tmp_path / "cache").glob("*.git"))
