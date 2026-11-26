from __future__ import annotations

from pathlib import Path

import pytest

from airtap_sync.cli import EXIT_OK, main
from airtap_sync.gitlab.check import Status, run_checks
from airtap_sync.gitlab.client import GitLabClient
from airtap_sync.gitlab.credentials import FileTokenStore
from airtap_sync.gitlab.lfs import LfsClient, LfsObject
from airtap_sync.gitsync import context
from airtap_sync.gitsync.context import GitContext
from airtap_sync.gitsync.gitcmd import GitRunner
from tests.gitlab.fakegitlab import FakeGitLab
from tests.gitsync.gitrepo import Remote


def checks(fake: FakeGitLab, remotes: dict[str, Remote], token: str = "secret"):
    return {
        r.name: r
        for r in run_checks(
            GitLabClient(fake.url, token),
            GitRunner(),
            lambda path: remotes[path].url,
            LfsClient(fake.url, token),
            sample=5,
            base_url=fake.url,
        )
    }


@pytest.fixture
def populated(tmp_path: Path, fake_gitlab: FakeGitLab) -> dict[str, Remote]:
    remotes = {}
    for i, path in enumerate(["team/a", "team/b"], start=1):
        remotes[path] = Remote(tmp_path, path.replace("/", "-"))
        remotes[path].commit("f", path)
        fake_gitlab.add_project(i, path)
    return remotes


def test_all_checks_pass(fake_gitlab: FakeGitLab, populated: dict[str, Remote]) -> None:
    results = checks(fake_gitlab, populated)
    assert {r.status for r in results.values()} == {Status.OK}
    assert "13.1.3-ee" in results["GitLab 版本"].detail
    assert "2 个工程" in results["列出工程"].detail
    assert results["LFS 接口"].detail.startswith("team/")
    assert "尚无 LFS 数据" in results["LFS 接口"].detail


def test_lfs_probe_prefers_a_project_that_uses_lfs(
    fake_gitlab: FakeGitLab, populated: dict[str, Remote]
) -> None:
    fake_gitlab.projects[1]["statistics"]["lfs_objects_size"] = 123
    detail = checks(fake_gitlab, populated)["LFS 接口"].detail
    assert detail.startswith("team/b（使用 LFS 的工程）")


def test_bad_token_stops_early(
    fake_gitlab: FakeGitLab, populated: dict[str, Remote]
) -> None:
    results = checks(fake_gitlab, populated, token="wrong")
    assert results["访问令牌"].status is Status.FAIL
    assert "列出工程" not in results


def test_non_admin_and_other_version_are_flagged(
    fake_gitlab: FakeGitLab, populated: dict[str, Remote]
) -> None:
    fake_gitlab.is_admin = False
    fake_gitlab.version = "15.0.0"
    results = checks(fake_gitlab, populated)
    assert results["管理员权限"].status is Status.WARN
    assert results["GitLab 版本"].status is Status.WARN


def test_git_failures_are_reported(
    tmp_path: Path, fake_gitlab: FakeGitLab, populated: dict[str, Remote]
) -> None:
    broken = {p: Remote(tmp_path, "x" + p.replace("/", "-")) for p in populated}
    for i, r in enumerate(broken.values()):
        r.bare.rename(r.bare.with_name(f"gone{i}"))
    results = checks(fake_gitlab, broken)
    assert results["git 读取（令牌能否通过 HTTP 访问代码）"].status is Status.FAIL


def test_lfs_not_enabled(fake_gitlab: FakeGitLab, populated: dict[str, Remote]) -> None:
    for p in fake_gitlab.projects:
        p["lfs_enabled"] = False
    assert checks(fake_gitlab, populated)["LFS 接口"].status is Status.WARN


def test_foreign_external_url_warns(
    fake_gitlab: FakeGitLab, populated: dict[str, Remote]
) -> None:
    fake_gitlab.projects[0]["http_url_to_repo"] = "http://other-host.invalid/team/a.git"
    result = checks(fake_gitlab, populated)["external_url"]
    assert result.status is Status.WARN
    assert "other-host.invalid" in result.detail


def test_external_url_alias_of_the_same_machine_is_fine(
    fake_gitlab: FakeGitLab, populated: dict[str, Remote]
) -> None:
    for p in fake_gitlab.projects:  # "localhost" resolves like 127.0.0.1
        p["http_url_to_repo"] = p["http_url_to_repo"].replace("127.0.0.1", "localhost")
    result = checks(fake_gitlab, populated)["external_url"]
    assert result.status is Status.OK and "同一台机器" in result.detail


def test_lfs_batch_roundtrip_and_errors(fake_gitlab: FakeGitLab) -> None:
    fake_gitlab.add_project(1, "team/a")
    lfs = LfsClient(fake_gitlab.url, "secret")
    answer = lfs.batch("team/a", "download", [LfsObject("ab" * 32, 5)])
    assert answer[0]["oid"] == "ab" * 32
    assert answer[0]["error"]["code"] == 404  # unknown object, but the call works
    from airtap_sync.gitlab.client import GitLabNotFound

    with pytest.raises(GitLabNotFound):
        lfs.batch("team/unknown", "download", [LfsObject("ab" * 32, 5)])


def test_cli_check(
    tmp_path: Path,
    fake_gitlab: FakeGitLab,
    populated: dict[str, Remote],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'side = "A"\npair_id = "p"\n[gitlab]\nurl = "{fake_gitlab.url}"\n'
        f'[gitlab.cache]\ndir = "{(tmp_path / "cache").as_posix()}"\n',
        encoding="utf-8",
    )
    store = FileTokenStore(tmp_path / "tok.json")
    store.set(fake_gitlab.url, "secret")
    monkeypatch.setattr(context, "load_token", lambda url, s=None: store.get(url))
    from airtap_sync.cli import load_token  # noqa: F401

    monkeypatch.setattr("airtap_sync.cli.load_token", lambda url, s=None: "secret")
    monkeypatch.setattr(GitContext, "clone_url", lambda self, path: populated[path].url)

    assert main(["--config", str(config), "gitlab", "check"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "[通过] 访问令牌" in out and "[失败]" not in out

    fake_gitlab.is_admin = False
    assert main(["--config", str(config), "gitlab", "check"]) == EXIT_OK
    assert "[警告] 管理员权限" in capsys.readouterr().out

    fake_gitlab.projects[0]["http_url_to_repo"] = "http://unresolvable.invalid/x.git"
    fake_gitlab.is_admin = True
    assert main(["--config", str(config), "gitlab", "check"]) == EXIT_OK
    assert "[警告] external_url" in capsys.readouterr().out


def test_access_levels_are_summarised(
    fake_gitlab: FakeGitLab, populated: dict[str, Remote]
) -> None:
    perms = lambda level: {  # noqa: E731
        "project_access": None,
        "group_access": {"access_level": level},
    }
    fake_gitlab.projects[0]["permissions"] = perms(40)
    fake_gitlab.projects[1]["permissions"] = perms(20)
    fake_gitlab.is_admin = True
    result = checks(fake_gitlab, populated)["项目权限"]
    assert result.status is Status.OK  # an admin can push everywhere
    assert "管理员 2" in result.detail
    fake_gitlab.is_admin = False
    result = checks(fake_gitlab, populated)["项目权限"]
    assert result.status is Status.WARN
    assert "1/2" in result.detail and "Maintainer 1，只读 1" in result.detail
    assert "team（1）" in result.detail


def test_non_members_are_read_only_for_non_admins(
    fake_gitlab: FakeGitLab, populated: dict[str, Remote]
) -> None:
    fake_gitlab.is_admin = False  # permissions absent -> not a member
    result = checks(fake_gitlab, populated)["项目权限"]
    assert result.status is Status.WARN
    assert "2/2" in result.detail and "只读 2" in result.detail


def test_namespace_table_breaks_down_mixed_namespaces() -> None:
    from airtap_sync.gitlab.check import namespace_table
    from airtap_sync.gitlab.projects import parse_project

    def project(path: str, level: int | None):
        perms = None if level is None else {"group_access": {"access_level": level}}
        return parse_project(
            {"id": 1, "path": "x", "path_with_namespace": path, "permissions": perms}
        )

    projects = [
        project("ZH/mine/a", 50),
        project("ZH/mine/b", 50),
        project("ZH/other/c", None),
        project("ZH/top", None),
        project("Tegra/x", None),
        project("own/y", 40),
    ]
    lines = namespace_table(projects, is_admin=False)
    assert lines[0] == "ZH/*  可写 2 / 共 4"  # most writable first
    assert "    ZH/mine/*  可写 2 / 共 2" in lines
    assert not any("ZH/other" in line for line in lines)  # nothing writable there
    assert "own/*  可写 1 / 共 1" in lines
    assert "Tegra/*  可写 0 / 共 1" in lines
    assert lines.index("own/*  可写 1 / 共 1") < lines.index("Tegra/*  可写 0 / 共 1")


def test_cli_check_namespaces_flag(
    tmp_path: Path,
    fake_gitlab: FakeGitLab,
    populated: dict[str, Remote],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'side = "A"\npair_id = "p"\n[gitlab]\nurl = "{fake_gitlab.url}"\n'
        f'[gitlab.cache]\ndir = "{(tmp_path / "cache").as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(context, "load_token", lambda url, s=None: "secret")
    monkeypatch.setattr("airtap_sync.cli.load_token", lambda url, s=None: "secret")
    monkeypatch.setattr(GitContext, "clone_url", lambda self, path: populated[path].url)
    fake_gitlab.is_admin = False
    main(["--config", str(config), "gitlab", "check", "--namespaces"])
    out = capsys.readouterr().out
    assert "各命名空间中本账号可推送的工程数" in out
    assert "team/*  可写 0 / 共 2" in out


def test_roles_inherited_from_a_parent_group_count_as_writable(
    fake_gitlab: FakeGitLab, populated: dict[str, Remote]
) -> None:
    """The project list shows no role for a project whose group inherits it
    (``permissions`` is null), yet the user is Owner through the parent group."""
    fake_gitlab.is_admin = False
    fake_gitlab.projects[0]["permissions"] = {
        "project_access": None,
        "group_access": None,
    }
    fake_gitlab.projects[0]["inherited_level"] = 50
    result = checks(fake_gitlab, populated)["项目权限"]
    assert "Owner 1" in result.detail
    assert "1/2" in result.detail  # only the other project is read-only
