"""Tray icon, progress window and dialogs (pystray + tkinter).

The toolkits are imported lazily: they are optional, and importing them on a
machine without a display fails. All widgets live on the main thread; worker
threads talk to it through a queue (``post``), so this module is the only
place that touches a toolkit and stays thin.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from airtap_sync import paths
from airtap_sync.agent.controller import AgentController, Status
from airtap_sync.progress import ProgressEvent

log = logging.getLogger(__name__)

STATUS_COLORS: dict[Status, str] = {
    Status.IDLE: "#7a8794",
    Status.SYNCING: "#2f7de1",
    Status.OK: "#2e9e50",
    Status.ATTENTION: "#e6992e",
    Status.ERROR: "#d9443b",
    Status.PAUSED: "#b9c0c7",
}
PUMP_MS = 100


def gui_available() -> tuple[bool, str]:
    """Whether tkinter and pystray can be used here, and why not if they cannot."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False, "缺少 tkinter（Linux 请安装 python3-tk）"
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False, "没有图形显示环境"
    try:
        import PIL.Image  # noqa: F401
        import pystray  # noqa: F401
    except Exception as e:  # pystray raises non-ImportErrors without a display
        return False, f"缺少托盘组件（pip install airtap-sync[tray]）：{e}"
    return True, ""


def open_path(path: Path) -> None:
    """Open a file or folder with the platform's default application."""
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        log.warning("cannot open %s", path, exc_info=True)


def make_icons(color_by_status: dict[Status, str]) -> dict[Status, Any]:
    """One round icon per status, drawn with Pillow."""
    from PIL import Image, ImageDraw

    icons = {}
    for status, color in color_by_status.items():
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((4, 4, 60, 60), fill=color)
        draw.arc((16, 16, 48, 48), 40, 300, fill="white", width=5)
        draw.polygon([(40, 12), (52, 24), (36, 26)], fill="white")
        icons[status] = image
    return icons


class GuiUi:
    """Implements the controller's ``Ui`` with a tray icon and a progress window."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._controller: AgentController | None = None
        self._stop = threading.Event()

    # ----- Ui protocol (called from any thread) ------------------------------------

    def set_status(self, status: Status, text: str) -> None:
        self._queue.put(("status", status, text))

    def progress(self, event: ProgressEvent) -> None:
        self._queue.put(("progress", event))

    def notify(self, title: str, message: str) -> None:
        self._queue.put(("notify", title, message))

    def confirm(self, title: str, message: str) -> bool:
        answer: queue.Queue[bool] = queue.Queue(maxsize=1)
        self._queue.put(("confirm", title, message, answer))
        return answer.get()

    # ----- main thread ---------------------------------------------------------------

    def run(self, controller: AgentController) -> None:
        """Show the tray icon and run until the user quits."""
        import tkinter as tk
        from tkinter import messagebox, ttk

        import pystray

        self._controller = controller
        root = tk.Tk()
        root.withdraw()
        window = _ProgressWindow(tk, ttk, root)
        icons = make_icons(STATUS_COLORS)

        def post(*item: Any) -> Callable[..., None]:
            return lambda *_args: self._queue.put(item)

        menu = pystray.Menu(
            pystray.MenuItem(
                "立即同步", lambda *_: controller.sync_now(), default=True
            ),
            pystray.MenuItem("打开最近报告", post("report")),
            pystray.MenuItem("打开日志目录", post("logs")),
            pystray.MenuItem(
                "暂停自动同步",
                post("toggle-pause"),
                checked=lambda _item: controller.paused,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", post("quit")),
        )
        icon = pystray.Icon("airtap-sync", icons[Status.IDLE], "airtap-sync", menu)
        icon.run_detached()

        def pump() -> None:
            try:
                while True:
                    item = self._queue.get_nowait()
                    if not self._handle(item, icon, icons, window, messagebox):
                        return
            except queue.Empty:
                pass
            root.after(PUMP_MS, pump)

        watcher = threading.Thread(
            target=controller.run_forever, args=(self._stop,), daemon=True
        )
        watcher.start()
        root.after(PUMP_MS, pump)
        controller.ui.set_status(Status.IDLE, "等待 U 盘")
        try:
            root.mainloop()
        finally:
            self._stop.set()
            icon.stop()

    def _handle(
        self,
        item: tuple[Any, ...],
        icon: Any,
        icons: dict,
        window: Any,
        messagebox: Any,
    ) -> bool:
        """Process one queued message; ``False`` means: quit."""
        kind = item[0]
        controller = self._controller
        assert controller is not None
        if kind == "status":
            _, status, text = item
            icon.icon = icons[status]
            icon.title = f"airtap-sync：{text}"
            window.set_status(status, text)
        elif kind == "progress":
            window.update_progress(item[1])
        elif kind == "notify":
            try:
                icon.notify(item[2], item[1])
            except Exception:
                log.debug("tray notification failed", exc_info=True)
        elif kind == "confirm":
            _, title, message, answer = item
            window.show()
            answer.put(bool(messagebox.askyesno(title, message)))
        elif kind == "report":
            if controller.last_report:
                open_path(controller.last_report)
        elif kind == "logs":
            open_path(paths.log_dir())
        elif kind == "toggle-pause":
            controller.resume() if controller.paused else controller.pause()
            icon.update_menu()
        elif kind == "quit":
            window.destroy()
            self._stop.set()
            icon.stop()
            window.root.quit()
            return False
        return True


class _ProgressWindow:
    """A small window with the current stage and a progress bar."""

    def __init__(self, tk: Any, ttk: Any, root: Any) -> None:
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.title("airtap-sync")
        self.window.geometry("460x150")
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.status = tk.StringVar(value="")
        self.detail = tk.StringVar(value="")
        ttk.Label(self.window, textvariable=self.status, font=("", 11, "bold")).pack(
            anchor="w", padx=14, pady=(14, 2)
        )
        ttk.Label(self.window, textvariable=self.detail, wraplength=430).pack(
            anchor="w", padx=14
        )
        self.bar = ttk.Progressbar(self.window, length=430, mode="indeterminate")
        self.bar.pack(padx=14, pady=12)
        ttk.Button(self.window, text="关闭", command=self.hide).pack(
            anchor="e", padx=14
        )
        self.window.withdraw()

    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()

    def hide(self) -> None:
        self.window.withdraw()

    def destroy(self) -> None:
        self.window.destroy()

    def set_status(self, status: Status, text: str) -> None:
        self.status.set(text)
        if status is Status.SYNCING:
            self.detail.set("")
            self.bar.configure(mode="indeterminate")
            self.bar.start(60)
            self.show()
        elif status in (Status.OK, Status.ATTENTION, Status.ERROR):
            self.bar.stop()
            self.bar.configure(
                mode="determinate", value=100 if status is Status.OK else 0
            )
            self.show()

    def update_progress(self, event: ProgressEvent) -> None:
        self.status.set(f"{event.stage.label}")
        self.detail.set(event.message)
        if event.total:
            self.bar.stop()
            self.bar.configure(
                mode="determinate", maximum=event.total, value=event.done
            )
        else:
            self.bar.configure(mode="indeterminate")
            self.bar.start(60)
