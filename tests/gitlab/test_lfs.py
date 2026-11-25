from __future__ import annotations

from pathlib import Path

import pytest

from airtap_sync.gitlab.client import GitLabError, GitLabNotFound
from airtap_sync.gitlab.lfs import LfsClient, LfsObject
from tests.gitlab.fakegitlab import FakeGitLab


@pytest.fixture
def lfs(fake_gitlab: FakeGitLab) -> LfsClient:
    fake_gitlab.add_project(1, "team/a")
    fake_gitlab.add_project(2, "team/b")
    return LfsClient(fake_gitlab.url, "secret")


def test_download_follows_the_configured_host_not_the_reported_one(
    fake_gitlab: FakeGitLab, lfs: LfsClient, tmp_path: Path
) -> None:
    oid, size = fake_gitlab.lfs_put("team/a", b"large file content")
    dest = tmp_path / "out" / "obj"
    lfs.download("team/a", LfsObject(oid, size), dest)
    assert dest.read_bytes() == b"large file content"
    # The batch answer pointed at http://external.invalid; the GET still arrived.
    assert ("LFS-GET", f"/team/a.git/gitlab-lfs/objects/{oid}") in fake_gitlab.requests
    assert not list(dest.parent.glob(".*.tmp"))


def test_download_of_a_missing_object(lfs: LfsClient, tmp_path: Path) -> None:
    with pytest.raises(GitLabNotFound, match="不存在"):
        lfs.download("team/a", LfsObject("0" * 64, 3), tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_download_detects_corruption(
    fake_gitlab: FakeGitLab, lfs: LfsClient, tmp_path: Path
) -> None:
    oid, size = fake_gitlab.lfs_put("team/a", b"abc")
    fake_gitlab.lfs["team/a"][oid] = b"xyz"  # the server returns other bytes
    with pytest.raises(GitLabError, match="校验"):
        lfs.download("team/a", LfsObject(oid, size), tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_upload_new_object_then_verify(
    fake_gitlab: FakeGitLab, lfs: LfsClient, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"payload" * 1000)
    oid, size = fake_gitlab.lfs_put("team/other", src.read_bytes())
    fake_gitlab.lfs.pop("team/other")
    obj = LfsObject(oid, size)

    assert lfs.missing("team/b", [obj]) == [obj]
    lfs.upload("team/b", obj, src)
    assert fake_gitlab.lfs["team/b"][oid] == src.read_bytes()
    kinds = [r[0] for r in fake_gitlab.requests if r[0].startswith("LFS-")]
    assert kinds == ["LFS-PUT", "LFS-VERIFY"]
    assert lfs.missing("team/b", [obj]) == []


def test_upload_of_an_object_the_project_has_does_nothing(
    fake_gitlab: FakeGitLab, lfs: LfsClient, tmp_path: Path
) -> None:
    oid, size = fake_gitlab.lfs_put("team/a", b"already there")
    src = tmp_path / "src"
    src.write_bytes(b"already there")
    lfs.upload("team/a", LfsObject(oid, size), src)
    assert not [r for r in fake_gitlab.requests if r[0] == "LFS-PUT"]


def test_upload_rejected_content(
    fake_gitlab: FakeGitLab, lfs: LfsClient, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"not what the oid says")
    with pytest.raises(GitLabError, match="PUT"):
        lfs.upload("team/a", LfsObject("1" * 64, src.stat().st_size), src)


def test_missing_with_no_objects(lfs: LfsClient) -> None:
    assert lfs.missing("team/a", []) == []
