"""The outcome of a directory sync run."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FolderReport:
    name: str
    scanned: int = 0
    fetched: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # "path → copy (winner)"
    deferred: list[str] = field(default_factory=list)  # waiting for data
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pending_deletes: list[str] = field(default_factory=list)
    exported: int = 0
    exported_bytes: int = 0
    unsent: int = 0  # files not sent this run for lack of space


@dataclass
class FilesReport:
    folders: list[FolderReport] = field(default_factory=list)
    dry_run: bool = False

    @property
    def needs_attention(self) -> bool:
        return any(f.conflicts or f.failures or f.pending_deletes for f in self.folders)

    @property
    def has_pending_confirmation(self) -> bool:
        return any(f.pending_deletes for f in self.folders)

    def summary(self) -> str:
        def total(attr: str) -> int:
            return sum(len(getattr(f, attr)) for f in self.folders)

        files = sum(f.scanned for f in self.folders)
        parts = [
            f"目录 {len(self.folders)} 个，文件 {files} 个",
            f"接收 {total('fetched')}",
            f"删除 {total('deleted')}",
            f"导出 {sum(f.exported for f in self.folders)}",
        ]
        for label, count in (
            ("冲突", total("conflicts")),
            ("失败", total("failures")),
            ("待确认", total("pending_deletes")),
            ("等待数据", total("deferred")),
        ):
            if count:
                parts.append(f"{label} {count}")
        return "，".join(parts)


def _lines(title: str, items: list[str], limit: int = 100) -> list[str]:
    if not items:
        return []
    out = [f"#### {title}（{len(items)}）", ""]
    out += [f"- {item}" for item in items[:limit]]
    if len(items) > limit:
        out.append(f"- …另有 {len(items) - limit} 项，详见日志")
    return [*out, ""]


def render_markdown(report: FilesReport) -> list[str]:
    """Markdown lines for the directory part of a sync report."""
    lines = ["## 目录同步" + ("（试运行）" if report.dry_run else ""), ""]
    lines += [f"概要：{report.summary()}", ""]
    for f in report.folders:
        lines += [f"### 目录 {f.name}", ""]
        lines += _lines("冲突（已保留两份）", f.conflicts)
        lines += _lines("需要人工确认：删除文件", f.pending_deletes)
        lines += _lines("失败", f.failures)
        lines += _lines("警告", f.warnings)
        lines += _lines("已接收或更新", f.fetched)
        lines += _lines("已删除（先移入回收区）", f.deleted)
        lines += _lines("等待数据（下一轮继续）", f.deferred)
        if f.unsent:
            lines += [f"本轮因空间不足未发送 {f.unsent} 个文件。", ""]
    return lines
