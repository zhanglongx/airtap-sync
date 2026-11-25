"""A text-only ``Ui``: used without a display, or with ``agent --no-gui``."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import TextIO

from airtap_sync.agent.controller import Status
from airtap_sync.progress import ConsoleProgress, ProgressEvent

log = logging.getLogger(__name__)


class ConsoleUi:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stderr
        self._progress = ConsoleProgress(self._stream)

    def set_status(self, status: Status, text: str) -> None:
        print(f"[{status.value}] {text}", file=self._stream)

    def progress(self, event: ProgressEvent) -> None:
        self._progress(event)

    def notify(self, title: str, message: str) -> None:
        print(f"== {title}\n{message}", file=self._stream)
        tool = shutil.which("notify-send")
        if tool:  # best effort desktop notification on Linux
            try:
                subprocess.run([tool, title, message], timeout=5, check=False)
            except (OSError, subprocess.SubprocessError):
                log.debug("notify-send failed", exc_info=True)

    def confirm(self, title: str, message: str) -> bool:
        print(f"== {title}\n{message}", file=self._stream)
        if not sys.stdin.isatty():
            print("非交互环境，按“否”处理", file=self._stream)
            return False
        return input("输入 yes 确认：").strip().lower() == "yes"
