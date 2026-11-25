"""Progress events emitted by the sync engine.

The engine only talks to a ``ProgressCallback``; the CLI and the GUI render the
events themselves, so the engine never depends on a UI library.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TextIO


class Stage(StrEnum):
    """Coarse phases of one sync run, in execution order."""

    SCAN = "scan"
    IMPORT = "import"
    MERGE = "merge"
    PUSH = "push"
    EXPORT = "export"
    DONE = "done"

    @property
    def label(self) -> str:
        return _STAGE_LABELS[self]


_STAGE_LABELS: dict[Stage, str] = {
    Stage.SCAN: "扫描",
    Stage.IMPORT: "导入",
    Stage.MERGE: "合并",
    Stage.PUSH: "推送",
    Stage.EXPORT: "导出",
    Stage.DONE: "完成",
}


@dataclass(frozen=True)
class ProgressEvent:
    """A single progress update.

    ``done`` and ``total`` count bytes or items depending on ``unit``;
    ``total`` is ``None`` while the amount of work is still unknown.
    """

    stage: Stage
    message: str = ""
    done: int = 0
    total: int | None = None
    unit: str = "items"

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return min(self.done / self.total, 1.0)


ProgressCallback = Callable[[ProgressEvent], None]


def null_progress(event: ProgressEvent) -> None:
    """Progress callback that discards every event."""


class ConsoleProgress:
    """Render progress events as plain lines on a text stream."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stderr

    def __call__(self, event: ProgressEvent) -> None:
        parts = [f"[{event.stage.label}]"]
        if event.total:
            parts.append(f"{event.done}/{event.total} {event.unit}")
        if event.message:
            parts.append(event.message)
        print(" ".join(parts), file=self._stream)
