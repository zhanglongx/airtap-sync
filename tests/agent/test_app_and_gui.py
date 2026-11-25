from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from airtap_sync.agent import app, gui
from airtap_sync.agent.console import ConsoleUi
from airtap_sync.agent.controller import AgentController, Status
from airtap_sync.agent.gui import GuiUi, gui_available, make_icons
from airtap_sync.agent.lock import SingleInstance
from airtap_sync.cli import EXIT_ERROR, EXIT_OK, main
from airtap_sync.progress import ProgressEvent, Stage
from airtap_sync.resources import icon_png

# ----- app ---------------------------------------------------------------------


def test_falls_back_to_text_mode_without_a_gui(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui, "gui_available", lambda: (False, "没有图形显示环境"))
    ui, use_gui = app._choose_ui(want_gui=True)
    assert isinstance(ui, ConsoleUi) and not use_gui
    ui, use_gui = app._choose_ui(want_gui=False)
    assert isinstance(ui, ConsoleUi) and not use_gui


def test_second_agent_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    holder = SingleInstance(app.lock_path())
    assert holder.acquire()
    try:
        assert app.run_agent(tmp_path / "config.toml", gui=False) == EXIT_ERROR
    finally:
        holder.release()
    assert "已经在运行" in capsys.readouterr().err


def test_console_agent_runs_until_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(self: AgentController, stop: threading.Event) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(AgentController, "run_forever", interrupted)
    assert app.run_agent(tmp_path / "config.toml", gui=False) == EXIT_OK
    # The lock was released on exit.
    again = SingleInstance(app.lock_path())
    assert again.acquire()
    again.release()


def test_cli_autostart_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from airtap_sync.agent import autostart

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(autostart, "_windows", lambda: False)
    assert main(["autostart", "status"]) == EXIT_OK
    assert "未启用" in capsys.readouterr().out
    assert main(["autostart", "enable"]) == EXIT_OK
    assert (tmp_path / "autostart" / "airtap-sync.desktop").exists()
    assert main(["autostart", "status"]) == EXIT_OK
    assert capsys.readouterr().out.strip().endswith("已启用")
    assert main(["autostart", "disable"]) == EXIT_OK
    assert main(["autostart", "disable"]) == EXIT_OK
    assert "本来就没有" in capsys.readouterr().out


# ----- gui (logic only: the widgets are stubbed) ------------------------------------


def test_gui_availability_never_raises() -> None:
    ok, reason = gui_available()
    assert isinstance(ok, bool)
    assert ok or reason


def test_icons_exist_for_every_status() -> None:
    pytest.importorskip("PIL")
    icons = make_icons(gui.STATUS_COLORS, icon_png())
    assert set(icons) == set(Status)
    assert all(img.size == (64, 64) for img in icons.values())
    # Each status is told apart by its dot; idle is the plain icon.
    assert icons[Status.OK].tobytes() != icons[Status.ERROR].tobytes()
    assert icons[Status.IDLE].tobytes() != icons[Status.OK].tobytes()


def test_icons_fall_back_to_discs_without_the_png() -> None:
    pytest.importorskip("PIL")
    icons = make_icons(gui.STATUS_COLORS, None)
    assert set(icons) == set(Status)
    assert icons[Status.OK].getpixel((32, 32)) != icons[Status.ERROR].getpixel((32, 32))


def test_confirm_blocks_until_the_main_thread_answers() -> None:
    ui = GuiUi()
    result: queue.Queue[bool] = queue.Queue()
    threading.Thread(
        target=lambda: result.put(ui.confirm("t", "m")), daemon=True
    ).start()
    kind, title, message, answer = ui._queue.get(timeout=5)
    assert (kind, title, message) == ("confirm", "t", "m")
    assert result.empty()  # still waiting
    answer.put(True)
    assert result.get(timeout=5) is True


class StubWindow:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.root = SimpleNamespace(quit=lambda: self.calls.append(("root.quit",)))

    def set_status(self, status, text):
        self.calls.append(("status", status, text))

    def update_progress(self, event):
        self.calls.append(("progress", event.stage))

    def show(self):
        self.calls.append(("show",))

    def destroy(self):
        self.calls.append(("destroy",))


class StubIcon:
    def __init__(self) -> None:
        self.icon = None
        self.title = ""
        self.notices: list[tuple[str, str]] = []
        self.stopped = False
        self.menu_updates = 0

    def notify(self, message, title):
        self.notices.append((title, message))

    def stop(self):
        self.stopped = True

    def update_menu(self):
        self.menu_updates += 1


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    opened: list[Path] = []
    monkeypatch.setattr(gui, "open_path", opened.append)
    ui = GuiUi()
    controller = SimpleNamespace(
        last_report=tmp_path / "r.md",
        paused=False,
        pause=lambda: setattr(controller, "paused", True),
        resume=lambda: setattr(controller, "paused", False),
    )
    ui._controller = controller  # type: ignore[assignment]
    return ui, controller, StubIcon(), StubWindow(), opened


def test_status_updates_icon_title_and_window(harness) -> None:
    ui, _c, icon, window, _o = harness
    icons = {Status.SYNCING: "img"}
    assert ui._handle(("status", Status.SYNCING, "正在同步"), icon, icons, window, None)
    assert icon.icon == "img" and "正在同步" in icon.title
    assert ("status", Status.SYNCING, "正在同步") in window.calls


def test_progress_notify_confirm_and_menu_actions(harness) -> None:
    ui, controller, icon, window, opened = harness
    ui._handle(("progress", ProgressEvent(Stage.EXPORT)), icon, {}, window, None)
    assert ("progress", Stage.EXPORT) in window.calls

    ui._handle(("notify", "标题", "内容"), icon, {}, window, None)
    assert icon.notices == [("标题", "内容")]

    answer: queue.Queue[bool] = queue.Queue(maxsize=1)
    box = SimpleNamespace(askyesno=lambda title, message: True)
    ui._handle(("confirm", "t", "m", answer), icon, {}, window, box)
    assert answer.get_nowait() is True
    assert ("show",) in window.calls  # the window is raised for the question

    ui._handle(("report",), icon, {}, window, None)
    assert opened == [controller.last_report]
    controller.last_report = None
    ui._handle(("report",), icon, {}, window, None)  # nothing to open: no error
    ui._handle(("logs",), icon, {}, window, None)
    assert len(opened) == 2

    ui._handle(("toggle-pause",), icon, {}, window, None)
    assert controller.paused and icon.menu_updates == 1
    ui._handle(("toggle-pause",), icon, {}, window, None)
    assert not controller.paused


def test_quit_stops_everything(harness) -> None:
    ui, _c, icon, window, _o = harness
    assert not ui._handle(("quit",), icon, {}, window, None)
    assert icon.stopped and ("destroy",) in window.calls
    assert ("root.quit",) in window.calls
    assert ui._stop.is_set()
