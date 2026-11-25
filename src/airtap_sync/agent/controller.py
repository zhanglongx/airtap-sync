"""The agent's logic, independent of any GUI toolkit.

A ``Ui`` shows status, progress, notifications and questions; the controller
watches for the paired drive, runs syncs on a worker thread and reports through
the ``Ui``. This split keeps everything but the thin tray/window code testable.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from airtap_sync.agent.watcher import find_volumes, mounted_roots
from airtap_sync.config import Config, ConfigError, load_config
from airtap_sync.progress import ProgressCallback, ProgressEvent
from airtap_sync.sync import SYNC_ERRORS, SyncOutcome, SyncResult, execute_sync

log = logging.getLogger(__name__)


class Status(StrEnum):
    IDLE = "idle"
    SYNCING = "syncing"
    OK = "ok"
    ATTENTION = "attention"
    ERROR = "error"
    PAUSED = "paused"


class Ui(Protocol):
    def set_status(self, status: Status, text: str) -> None: ...

    def progress(self, event: ProgressEvent) -> None: ...

    def notify(self, title: str, message: str) -> None: ...

    def confirm(self, title: str, message: str) -> bool:
        """Ask a yes/no question; called from the worker thread, may block."""
        ...


SyncRunner = Callable[..., SyncOutcome]


class AgentController:
    def __init__(
        self,
        config_path: Path,
        ui: Ui,
        *,
        runner: SyncRunner = execute_sync,
        roots: Callable[[], list[Path]] = mounted_roots,
    ) -> None:
        self.config_path = config_path
        self.ui = ui
        self._runner = runner
        self._roots = roots
        self.paused = False
        self.status = Status.IDLE
        self.last_report: Path | None = None
        self._present: set[Path] = set()
        self._handled: set[Path] = set()  # synced during this insertion
        self._lock = threading.Lock()
        self._busy = False
        self._thread: threading.Thread | None = None
        self._config_problem_shown = False

    # ----- configuration -------------------------------------------------------

    def load_config(self) -> Config | None:
        """The current config, re-read each time so edits apply without a restart."""
        try:
            config = load_config(self.config_path)
        except ConfigError as e:
            if not self._config_problem_shown:
                self._config_problem_shown = True
                self._set(Status.ERROR, "配置有误")
                self.ui.notify("配置错误", str(e))
            return None
        self._config_problem_shown = False
        return config

    # ----- watching ------------------------------------------------------------

    def poll_once(self) -> None:
        """Look at the mounted drives once; start a sync for a newly inserted one."""
        config = self.load_config()
        if config is None:
            return
        volumes = set(find_volumes(config.pair_id, self._roots))
        arrived = sorted(volumes - self._present)
        removed = self._present - volumes
        self._present = volumes
        self._handled -= removed
        if removed and self.status in (Status.OK, Status.ATTENTION, Status.ERROR):
            if not self._busy:
                self._set(Status.PAUSED if self.paused else Status.IDLE, "等待 U 盘")
        if self.paused:
            return
        for usb in arrived:
            if usb in self._handled:
                continue
            if config.agent.auto_sync or self._confirm_start(usb):
                self.start_sync(usb)

    def _confirm_start(self, usb: Path) -> bool:
        return self.ui.confirm("发现同步 U 盘", f"检测到 {usb}，现在开始同步吗？")

    def run_forever(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set (the agent's background loop)."""
        while not stop.is_set():
            self.poll_once()
            config = self.load_config()
            stop.wait(config.agent.poll_seconds if config else 3)

    # ----- running -------------------------------------------------------------

    def sync_now(self) -> bool:
        """Sync the currently inserted drive (the tray's "立即同步")."""
        config = self.load_config()
        if config is None:
            return False
        volumes = sorted(find_volumes(config.pair_id, self._roots))
        if not volumes:
            self.ui.notify("没有找到同步 U 盘", "请插入已初始化的 U 盘后再试")
            return False
        return self.start_sync(volumes[0])

    def start_sync(self, usb: Path) -> bool:
        """Start a sync on a worker thread; ``False`` if one is already running."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self._thread = threading.Thread(
            target=self._job, args=(usb,), name="airtap-sync", daemon=True
        )
        self._thread.start()
        return True

    def wait(self, timeout: float | None = None) -> None:
        """Block until the running sync (if any) finished."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _job(self, usb: Path) -> None:
        try:
            self._set(Status.SYNCING, f"正在同步 {usb}")
            config = self.load_config()
            if config is None:
                return
            progress: ProgressCallback = self.ui.progress
            try:
                outcome = self._runner(
                    config, usb, progress=progress, ask_confirmation=self._ask
                )
            except SYNC_ERRORS as e:
                self._set(Status.ERROR, "同步失败")
                self.ui.notify("同步失败", str(e))
            except Exception as e:  # keep the agent alive whatever happens
                log.exception("sync crashed")
                self._set(Status.ERROR, "同步出现意外错误")
                self.ui.notify("同步出现意外错误", f"{type(e).__name__}: {e}")
            else:
                self._finished(outcome)
            self._handled.add(usb)
        finally:
            with self._lock:
                self._busy = False

    def _finished(self, outcome: SyncOutcome) -> None:
        self.last_report = outcome.report_file
        summary = "\n".join(outcome.result.summaries())
        if outcome.result.needs_attention:
            self._set(Status.ATTENTION, "同步完成，有事项需要处理")
            self.ui.notify(
                "同步完成，但有事项需要处理",
                f"{summary}\n冲突、失败或待确认的内容见报告。可以拔出 U 盘。",
            )
        else:
            self._set(Status.OK, "同步完成")
            self.ui.notify("同步完成，可以拔出 U 盘", summary)

    def _ask(self, result: SyncResult) -> bool:
        items = result.pending_items()
        shown = "\n".join(items[:15]) + (
            f"\n…另有 {len(items) - 15} 项" if len(items) > 15 else ""
        )
        return self.ui.confirm(
            "需要确认", f"有 {len(items)} 项操作超过安全阈值，是否执行？\n\n{shown}"
        )

    # ----- user actions ----------------------------------------------------------

    def pause(self) -> None:
        self.paused = True
        if not self._busy:
            self._set(Status.PAUSED, "自动同步已暂停")

    def resume(self) -> None:
        self.paused = False
        if not self._busy:
            self._set(Status.IDLE, "等待 U 盘")
        # A drive that stayed in during the pause is picked up straight away.
        self._present.clear()

    def _set(self, status: Status, text: str) -> None:
        self.status = status
        self.ui.set_status(status, text)
