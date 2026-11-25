"""Collect repository size statistics from a GitLab instance (read-only).

Standalone helper used to size the airtap-sync git cache. Requires Python
3.8+ and only the standard library. Only ``GET /api/v4/projects`` is called.

Usage:
    python gitlab_stats.py --url https://gitlab.example.com [--insecure]
                           [--csv projects.csv]

The token is read from the ``GITLAB_TOKEN`` environment variable, or prompted
for interactively. An admin token sees every project on the instance.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

PER_PAGE = 100
TOP_N = 20
SIZE_BUCKETS: list[tuple[str, int]] = [
    ("< 10 MiB", 10 * 2**20),
    ("10–100 MiB", 100 * 2**20),
    ("100 MiB–1 GiB", 2**30),
    ("1–5 GiB", 5 * 2**30),
    ("5–20 GiB", 20 * 2**30),
    (">= 20 GiB", sys.maxsize),
]
ACTIVITY_WINDOWS_DAYS: list[int] = [1, 7, 30, 90]
STAT_KEYS: list[str] = [
    "repository_size",
    "lfs_objects_size",
    "wiki_size",
    "job_artifacts_size",
    "packages_size",
    "storage_size",
]


@dataclass
class Project:
    id: int
    path: str
    namespace_kind: str
    archived: bool
    last_activity_at: datetime | None
    stats: dict[str, int]

    @property
    def repo_size(self) -> int:
        return self.stats.get("repository_size", 0)


def human(n: float) -> str:
    """Format a byte count using binary units."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    raise AssertionError("unreachable")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_projects(
    base_url: str, token: str, ctx: ssl.SSLContext | None
) -> list[dict[str, Any]]:
    """Fetch all projects with statistics, following offset pagination."""
    projects: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {
                "statistics": "true",
                "per_page": PER_PAGE,
                "page": page,
                "order_by": "id",
                "sort": "asc",
            }
        )
        req = urllib.request.Request(
            f"{base_url}/api/v4/projects?{query}",
            headers={"PRIVATE-TOKEN": token},
        )
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            batch = json.load(resp)
            next_page = resp.headers.get("X-Next-Page", "")
            total = resp.headers.get("X-Total", "?")
        projects.extend(batch)
        print(f"  已获取 {len(projects)} / {total} 个工程", file=sys.stderr)
        if not next_page:
            return projects
        page = int(next_page)


def to_project(raw: dict[str, Any]) -> Project:
    stats_raw = raw.get("statistics") or {}
    stats = {k: int(stats_raw.get(k) or 0) for k in STAT_KEYS}
    return Project(
        id=int(raw["id"]),
        path=str(raw.get("path_with_namespace", raw["id"])),
        namespace_kind=str((raw.get("namespace") or {}).get("kind", "?")),
        archived=bool(raw.get("archived", False)),
        last_activity_at=parse_time(raw.get("last_activity_at")),
        stats=stats,
    )


def report(projects: list[Project]) -> None:
    now = datetime.now(timezone.utc)  # noqa: UP017 (keeps Python 3.8+ support)
    total_repo = sum(p.repo_size for p in projects)
    missing_stats = sum(1 for p in projects if not any(p.stats.values()))

    print("\n== 总体 ==")
    print(f"工程数: {len(projects)}")
    print(f"  归档工程: {sum(p.archived for p in projects)}")
    print(f"  个人命名空间: {sum(p.namespace_kind == 'user' for p in projects)}")
    empty = sum(p.repo_size == 0 for p in projects)
    print(f"  代码为空 (repository_size = 0): {empty}")
    if missing_stats:
        print(f"  无统计信息的工程: {missing_stats}（令牌权限可能不足）")

    print("\n== 各类存储合计 ==")
    for key in STAT_KEYS:
        print(f"{key:>20}: {human(sum(p.stats.get(key, 0) for p in projects))}")

    print("\n== 代码仓库大小分布 (repository_size) ==")
    lower = 0
    for label, upper in SIZE_BUCKETS:
        bucket = [p for p in projects if lower <= p.repo_size < upper]
        size = sum(p.repo_size for p in bucket)
        print(f"{label:>14}: {len(bucket):5d} 个, 合计 {human(size)}")
        lower = upper

    print(f"\n== 代码仓库最大的 {TOP_N} 个工程 ==")
    ranked = sorted(projects, key=lambda p: p.repo_size, reverse=True)
    for p in ranked[:TOP_N]:
        last = p.last_activity_at.date().isoformat() if p.last_activity_at else "-"
        print(f"{human(p.repo_size):>12}  {last}  {p.path}")
    cumulative = 0
    for i, p in enumerate(ranked, start=1):
        cumulative += p.repo_size
        if total_repo and cumulative >= total_repo * 0.8:
            print(f"最大的 {i} 个工程占代码总量的 80%")
            break

    print("\n== 活跃度 (last_activity_at，可能滞后 1 小时) ==")
    for days in ACTIVITY_WINDOWS_DAYS:
        since = now - timedelta(days=days)
        active = [
            p for p in projects if p.last_activity_at and p.last_activity_at >= since
        ]
        size = sum(p.repo_size for p in active)
        print(f"最近 {days:>2} 天: {len(active):5d} 个工程, 代码合计 {human(size)}")


def write_csv(path: str, projects: list[Project]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["id", "path", "namespace_kind", "archived", "last_activity_at", *STAT_KEYS]
        )
        for p in projects:
            last = p.last_activity_at.isoformat() if p.last_activity_at else ""
            writer.writerow(
                [
                    p.id,
                    p.path,
                    p.namespace_kind,
                    p.archived,
                    last,
                    *(p.stats.get(k, 0) for k in STAT_KEYS),
                ]
            )
    print(f"\n明细已写入 {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="统计 GitLab 工程代码规模（只读）")
    parser.add_argument(
        "--url", required=True, help="GitLab 地址，如 https://gitlab.example.com"
    )
    parser.add_argument("--insecure", action="store_true", help="忽略 HTTPS 证书错误")
    parser.add_argument("--csv", help="把每个工程的明细写入 CSV 文件")
    args = parser.parse_args()

    token = os.environ.get("GITLAB_TOKEN") or getpass.getpass("GitLab token: ")
    ctx = ssl._create_unverified_context() if args.insecure else None
    base_url = args.url.rstrip("/")

    try:
        raw = fetch_projects(base_url, token, ctx)
    except urllib.error.HTTPError as e:
        print(f"请求失败: HTTP {e.code} {e.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"无法连接 GitLab: {e.reason}", file=sys.stderr)
        return 1

    projects = [to_project(r) for r in raw]
    report(projects)
    if args.csv:
        write_csv(args.csv, projects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
