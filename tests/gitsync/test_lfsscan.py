from __future__ import annotations

from pathlib import Path

from airtap_sync.gitlab.lfs import LfsObject
from airtap_sync.gitsync.gitcmd import GitRunner
from airtap_sync.gitsync.lfsscan import find_lfs_objects, parse_pointer
from tests.gitsync.gitrepo import Remote


def pointer(oid: str, size: int) -> str:
    return (
        f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {size}\n"
    )


def test_parse_pointer() -> None:
    oid = "ab" * 32
    assert parse_pointer(pointer(oid, 12).encode()) == LfsObject(oid, 12)
    assert parse_pointer(b"just a file\n") is None
    assert parse_pointer(pointer("zz" * 32, 1).encode()) is None
    assert parse_pointer(pointer(oid, 1).encode() + b"extra\n") is None


def test_finds_pointers_in_new_commits_only(tmp_path: Path) -> None:
    runner = GitRunner()
    remote = Remote(tmp_path, "r")
    old = remote.commit("old.bin", pointer("11" * 32, 100))
    remote.commit("readme", "not a pointer")
    remote.commit("big", "x" * 5000)  # larger than any pointer
    remote.commit("new.bin", pointer("22" * 32, 200))
    remote.commit("same.bin", pointer("22" * 32, 200))  # duplicate oid
    tip = remote.rev("main")

    everything = find_lfs_objects(runner, remote.bare, [tip])
    assert everything == [LfsObject("11" * 32, 100), LfsObject("22" * 32, 200)]
    new_only = find_lfs_objects(runner, remote.bare, [tip], known=[old])
    assert new_only == [LfsObject("22" * 32, 200)]


def test_nothing_to_find(tmp_path: Path) -> None:
    runner = GitRunner()
    remote = Remote(tmp_path, "r")
    tip = remote.commit("f", "plain")
    assert find_lfs_objects(runner, remote.bare, []) == []
    assert find_lfs_objects(runner, remote.bare, [tip]) == []
    assert find_lfs_objects(runner, remote.bare, [tip], known=["0" * 40]) == []
