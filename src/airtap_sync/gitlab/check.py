"""Read-only diagnostics of a GitLab instance for airtap-sync (`gitlab check`)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse

from airtap_sync.gitlab.client import GitLabClient, GitLabError
from airtap_sync.gitlab.lfs import LfsClient
from airtap_sync.gitlab.projects import Project, current_user, list_projects
from airtap_sync.gitsync.gitcmd import GitError, GitRunner

# Version this program was designed and tested against.
EXPECTED_VERSION = "13.1"


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str


def run_checks(
    client: GitLabClient,
    runner: GitRunner,
    clone_url: Callable[[str], str],
    lfs: LfsClient,
    *,
    sample: int = 20,
    base_url: str,
) -> list[CheckResult]:
    """Run every check; a failing one never stops the others."""
    results: list[CheckResult] = []

    def add(name: str, status: Status, detail: str) -> None:
        results.append(CheckResult(name, status, detail))

    try:
        runner.check_version()
        add("git 版本", Status.OK, ".".join(map(str, runner.version())))
    except GitError as e:
        add("git 版本", Status.FAIL, str(e))

    try:
        user = current_user(client)
    except GitLabError as e:
        add("访问令牌", Status.FAIL, str(e))
        return results
    add("访问令牌", Status.OK, f"用户 {user.username}")
    if user.is_admin:
        add("管理员权限", Status.OK, "可以列出全部工程、在个人命名空间下建工程")
    else:
        add("管理员权限", Status.FAIL, "不是管理员：看不到全部工程，也无法代建个人工程")

    try:
        info = client.get("/version")
        version = str(info.get("version", "?"))
        status = Status.OK if version.startswith(EXPECTED_VERSION) else Status.WARN
        add("GitLab 版本", status, f"{version}（本程序按 {EXPECTED_VERSION}.x 设计）")
    except GitLabError as e:
        add("GitLab 版本", Status.WARN, str(e))

    started = time.monotonic()
    try:
        projects = list_projects(client)
    except GitLabError as e:
        add("列出工程", Status.FAIL, str(e))
        return results
    elapsed = time.monotonic() - started
    total = sum(p.repository_size for p in projects)
    add(
        "列出工程",
        Status.OK if projects else Status.WARN,
        f"{len(projects)} 个工程，代码共 {total / 2**30:.1f} GiB，"
        f"用时 {elapsed:.1f} 秒",
    )
    if not projects:
        return results

    results += _check_git(projects, runner, clone_url, lfs, sample, base_url)
    return results


def _check_git(
    projects: list[Project],
    runner: GitRunner,
    clone_url: Callable[[str], str],
    lfs: LfsClient,
    sample: int,
    base_url: str,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    # Prefer projects that have code, most recently active first.
    candidates = sorted(
        (p for p in projects if p.repository_size > 0),
        key=lambda p: p.last_activity_at or "",
        reverse=True,
    )[: max(1, sample)]
    if not candidates:
        candidates = projects[:sample]

    failures: list[str] = []
    timings: list[float] = []
    for project in candidates:
        started = time.monotonic()
        try:
            runner.ls_remote(clone_url(project.full_path))
        except GitError as e:
            failures.append(f"{project.full_path}: {e}")
        else:
            timings.append(time.monotonic() - started)
    if failures:
        results.append(
            CheckResult(
                "git 读取（令牌能否通过 HTTP 访问代码）",
                Status.FAIL,
                f"{len(failures)}/{len(candidates)} 个失败，例如 {failures[0]}。"
                "若是认证失败，令牌需要增加 read_repository/write_repository 权限",
            )
        )
    else:
        mean = sum(timings) / len(timings)
        estimate = mean * len(projects) / 8  # default concurrency
        results.append(
            CheckResult(
                "git 读取（令牌能否通过 HTTP 访问代码）",
                Status.OK,
                f"{len(candidates)} 个工程均成功，平均 {mean:.2f} 秒/个，"
                f"估计 {len(projects)} 个工程 8 路并发约 {estimate / 60:.1f} 分钟",
            )
        )

    host = urlparse(base_url).netloc
    foreign = [
        p.full_path
        for p in projects[:50]
        if p.http_url and urlparse(p.http_url).netloc != host
    ]
    if foreign:
        results.append(
            CheckResult(
                "external_url",
                Status.WARN,
                f"GitLab 报告的地址与配置的 {host} 不同（如 {foreign[0]}）。"
                "程序用配置的地址访问，但 LFS 的上传下载地址由服务端决定，需确认可达",
            )
        )
    else:
        results.append(CheckResult("external_url", Status.OK, "与配置的地址一致"))

    results.append(_check_lfs(projects, lfs))
    return results


def _check_lfs(projects: list[Project], lfs: LfsClient) -> CheckResult:
    target = next((p for p in projects if p.lfs_enabled), None)
    name = "LFS 接口"
    if target is None:
        return CheckResult(name, Status.WARN, "没有启用 LFS 的工程，无法检测")
    try:
        lfs.batch(target.full_path, "download", [])
    except GitLabError as e:
        return CheckResult(name, Status.FAIL, f"{target.full_path}: {e}")
    return CheckResult(name, Status.OK, f"{target.full_path} 的 Batch 接口可用")
