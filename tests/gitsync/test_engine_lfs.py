from __future__ import annotations

import os
from pathlib import Path

from airtap_sync.config import TransferConfig
from tests.gitlab.fakegitlab import FakeGitLab
from tests.gitsync.sites import Sites, seed

H = "refs/heads/"


def pointer(oid: str, size: int) -> str:
    return (
        f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {size}\n"
    )


def commit_lfs(sites: Sites, content: bytes, path: str = "team/app") -> str:
    """Commit a pointer on A and register the object in A's LFS store."""
    oid, size = sites["A"].gitlab.lfs_put(path, content)
    return sites["A"].remote(path).commit(f"big-{oid[:6]}.bin", pointer(oid, size))


def test_lfs_objects_travel_with_the_bundle(sites: Sites) -> None:
    seed(sites)
    content = os.urandom(3000)
    tip = commit_lfs(sites, content)
    oid = next(iter(sites["A"].gitlab.lfs["team/app"]))

    export = sites.run("A")
    assert export.failures == []
    assert (sites.volume.outbox_dir("A") / "blobs" / oid[:2] / oid).is_file()

    imported = sites.run("B")
    assert imported.failures == []
    assert sites["B"].heads("team/app")[H + "main"] == tip
    assert sites["B"].gitlab.lfs["team/app"][oid] == content
    # Uploaded before the refs were pushed (GitLab checks this on push).
    kinds = [r[0] for r in sites["B"].gitlab.requests if r[0].startswith("LFS")]
    assert "LFS-PUT" in kinds and "LFS-VERIFY" in kinds

    sites.run("A")  # B is up to date: bundle and blobs are cleaned off the drive
    blobs = sites.volume.outbox_dir("A") / "blobs"
    assert not blobs.exists() or not any(blobs.rglob("*"))


def test_a_new_project_gets_its_lfs_objects(sites: Sites) -> None:
    remote = sites["A"].add_project("team/new")
    content = os.urandom(1500)
    oid, size = sites["A"].gitlab.lfs_put("team/new", content)
    remote.commit("data.bin", pointer(oid, size))
    sites.run("A")
    report = sites.run("B")
    assert report.created == ["team/new"] and report.failures == []
    assert sites["B"].gitlab.lfs["team/new"][oid] == content


def test_objects_the_target_already_has_are_not_uploaded(sites: Sites) -> None:
    seed(sites)
    content = os.urandom(2000)
    commit_lfs(sites, content)
    oid, size = sites["B"].gitlab.lfs_put("team/app", content)  # already on B
    sites.run("A")
    sites.run("B")
    assert not [r for r in sites["B"].gitlab.requests if r[0] == "LFS-PUT"]
    assert (
        sites["B"].heads("team/app")[H + "main"]
        == sites["A"].heads("team/app")[H + "main"]
    )
    assert size == len(content) and oid


def test_unreadable_object_is_reported_on_the_sending_side(sites: Sites) -> None:
    seed(sites)
    sites["A"].remote("team/app").commit("ghost.bin", pointer("ee" * 32, 10))
    report = sites.run("A")
    assert any("无法读取" in f for f in report.failures)


def test_large_lfs_object_is_delivered_in_batches(
    tmp_path: Path,
    fake_gitlab: FakeGitLab,
    gitlab_b: FakeGitLab,
) -> None:
    sites = Sites(
        tmp_path,
        fake_gitlab,
        gitlab_b,
        transfer=TransferConfig(max_bytes=7_000, reserve_free=0, part_size=3_000),
    )
    seed(sites)
    content = os.urandom(15_000)
    tip = commit_lfs(sites, content)
    oid = next(iter(sites["A"].gitlab.lfs["team/app"]))

    waited = False
    for _ in range(12):
        sites.run("A")
        report = sites.run("B")
        assert report.failures == []
        waited |= any("LFS" in d for d in report.deferred)
        if sites["B"].heads("team/app").get(H + "main") == tip:
            break
    assert waited
    assert sites["B"].heads("team/app")[H + "main"] == tip
    assert sites["B"].gitlab.lfs["team/app"][oid] == content
