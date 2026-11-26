"""``airtap-sync status``: what is waiting to be synchronised, from local data only.

Nothing here touches the network or the USB drive, so it is quick and safe to
run at any time. Git changes on GitLab only show up in a real sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from airtap_sync import paths
from airtap_sync.config import Config, FolderConfig
from airtap_sync.filesync.ignore import IgnoreRules
from airtap_sync.filesync.scanner import scan_folder
from airtap_sync.filesync.state import FolderState
from airtap_sync.gitsync.state import GitState

CONFLICT_MARK = ".sync-conflict-"


@dataclass
class FolderStatus:
    name: str
    path: Path
    available: bool = True
    files: int = 0
    added: list[str] = field(default_factory=list)  # new since the last agreement
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflict_copies: list[str] = field(default_factory=list)
    synced_before: bool = False  # a base exists, i.e. at least one agreement


def folder_status(config: FolderConfig, state_root: Path) -> FolderStatus:
    """Local changes of a folder since both sides last agreed."""
    status = FolderStatus(config.name, config.path)
    if not config.path.is_dir():
        status.available = False
        return status
    state = FolderState(state_root / "files" / config.name / "state.sqlite")
    try:
        base = state.base()
        scan = scan_folder(config.path, IgnoreRules(config.ignore), state)
    finally:
        state.close()
    status.files = len(scan.files)
    status.synced_before = bool(base)
    for path, entry in scan.files.items():
        if path not in base:
            status.added.append(path)
        elif base[path] != entry.sha256:
            status.modified.append(path)
        if CONFLICT_MARK in path:
            status.conflict_copies.append(path)
    status.deleted = sorted(set(base) - set(scan.files))
    status.added.sort()
    status.modified.sort()
    return status


def last_report(log_dir: Path) -> tuple[Path, datetime] | None:
    """The newest sync report in the local log directory."""
    reports = sorted((log_dir / "reports").glob("*.md"))
    if not reports:
        return None
    newest = max(reports, key=lambda p: p.stat().st_mtime)
    return newest, datetime.fromtimestamp(newest.stat().st_mtime)


def _sample(items: list[str], limit: int = 5) -> str:
    shown = "、".join(items[:limit])
    return shown + (f" …共 {len(items)} 个" if len(items) > limit else "")


def render_status(config: Config, state_root: Path, log_dir: Path) -> list[str]:
    """Human-readable status lines."""
    lines = [f"本端：{config.side}（对端 {config.peer_side}）"]
    last = last_report(log_dir)
    if last:
        path, when = last
        lines.append(f"上次同步：{when:%Y-%m-%d %H:%M}（报告 {path}）")
    else:
        lines.append("上次同步：还没有同步过")

    for folder in config.folders.values():
        status = folder_status(folder, state_root)
        lines.append("")
        lines.append(f"目录 {status.name}（{status.path}）")
        if not status.available:
            lines.append("  目录不存在或不可读")
            continue
        lines.append(f"  共 {status.files} 个文件")
        if not status.synced_before:
            lines.append("  还没有同步基准：首次同步会先交换清单")
        pending = (
            ("新增", status.added),
            ("修改", status.modified),
            ("删除", status.deleted),
        )
        changes = [(label, items) for label, items in pending if items]
        if status.synced_before and not changes:
            lines.append("  相对上次同步没有本地改动")
        for label, items in changes:
            lines.append(f"  {label} {len(items)} 个：{_sample(items)}")
        if status.conflict_copies:
            lines.append(
                f"  有 {len(status.conflict_copies)} 个冲突副本待处理："
                f"{_sample(status.conflict_copies)}"
            )

    if config.gitlab is not None:
        lines.append("")
        lines.append(f"Git（{config.gitlab.url}）")
        db = state_root / "git" / "state.sqlite"
        if db.is_file():
            state = GitState(db)
            try:
                projects = len(state.base_projects())
            finally:
                state.close()
            lines.append(f"  已共有的工程：{projects} 个")
        else:
            lines.append(
                "  还没有基准：请先运行 git snapshot / git baseline，或直接同步"
            )
        lines.append("  GitLab 上的变化只有在同步时才会读取")
    return lines


def local_state_root() -> Path:
    return paths.state_dir()
