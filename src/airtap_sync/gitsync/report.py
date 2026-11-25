"""The outcome of a Git sync run, and its Markdown rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from airtap_sync.config import Side


@dataclass
class ExportedBundle:
    path: str
    size: int
    complete: bool
    full: bool


@dataclass
class GitSyncReport:
    side: Side
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)  # projects with ref changes
    refs_applied: int = 0
    refs_deleted: list[str] = field(default_factory=list)  # "project: ref"
    conflicts: list[str] = field(default_factory=list)  # "project: ref (note)"
    deferred: list[str] = field(default_factory=list)  # waiting for data
    failures: list[str] = field(default_factory=list)  # "project: message"
    notes: list[str] = field(default_factory=list)  # gone / renamed projects
    warnings: list[str] = field(default_factory=list)
    pending_deletes: list[str] = field(default_factory=list)
    pending_creates: list[str] = field(default_factory=list)
    exported: list[ExportedBundle] = field(default_factory=list)
    unsent: list[str] = field(default_factory=list)  # bundles not sent this run
    projects_total: int = 0
    projects_checked: int = 0
    budget_spent: int = 0
    budget_left: int = 0
    dry_run: bool = False

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.conflicts
            or self.failures
            or self.pending_deletes
            or self.pending_creates
            or self.notes
        )

    @property
    def has_pending_confirmation(self) -> bool:
        return bool(self.pending_deletes or self.pending_creates)

    def summary(self) -> str:
        parts = [
            f"检查 {self.projects_checked}/{self.projects_total} 个工程",
            f"新建 {len(self.created)}",
            f"更新 {len(self.updated)}",
            f"导出 {len(self.exported)}",
        ]
        for label, items in (
            ("冲突", self.conflicts),
            ("失败", self.failures),
            ("待确认", self.pending_deletes + self.pending_creates),
            ("等待数据", self.deferred),
        ):
            if items:
                parts.append(f"{label} {len(items)}")
        return "，".join(parts)


def write_report(report: GitSyncReport, directories: list[Path]) -> list[Path]:
    """Write the Markdown report into each directory; return the files written."""
    now = datetime.now(UTC)
    name = f"{now.strftime('%Y%m%d-%H%M%S')}-{report.side}-git.md"
    text = render_markdown(report, now)
    written = []
    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(text, encoding="utf-8")
            written.append(directory / name)
        except OSError:
            continue  # a read-only drive must not fail the whole sync
    return written


def _section(title: str, items: list[str], limit: int = 200) -> list[str]:
    if not items:
        return []
    lines = [f"## {title}（{len(items)}）", ""]
    lines += [f"- {item}" for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"- …另有 {len(items) - limit} 项，详见日志")
    return [*lines, ""]


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{n} B"
        value /= 1024
    raise AssertionError("unreachable")


def render_markdown(report: GitSyncReport, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S UTC")
    title = "Git 同步报告（试运行）" if report.dry_run else "Git 同步报告"
    lines = [f"# {title}", "", f"- 端：{report.side}", f"- 时间：{stamp}"]
    lines += [f"- 概要：{report.summary()}", ""]
    lines += _section("需要人工处理：冲突", report.conflicts)
    lines += _section("需要人工确认：删除分支或 tag", report.pending_deletes)
    lines += _section("需要人工确认：新建工程", report.pending_creates)
    lines += _section("失败", report.failures)
    lines += _section("工程变化（未自动处理）", report.notes)
    lines += _section("警告", report.warnings)
    lines += _section("新建的工程", report.created)
    lines += _section("已更新的工程", report.updated)
    lines += _section("已删除的分支或 tag", report.refs_deleted)
    lines += _section("等待数据（下一轮继续）", report.deferred)
    exported = [
        f"{b.path}：{_size(b.size)}，{'完整' if b.complete else '未传完'}"
        f"{'，全量' if b.full else ''}"
        for b in report.exported
    ]
    lines += _section("导出到 U 盘的 bundle", exported)
    lines += _section("本轮未发送（空间不足或待确认）", report.unsent)
    lines += [
        f"U 盘空间：本轮写入 {_size(report.budget_spent)}，"
        f"剩余配额 {_size(report.budget_left)}",
        "",
    ]
    return "\n".join(lines)
