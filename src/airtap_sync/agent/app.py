"""Starting the agent: lock, choose a UI, run until quit."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

from airtap_sync import __version__, paths
from airtap_sync.agent.console import ConsoleUi
from airtap_sync.agent.controller import AgentController, Ui
from airtap_sync.agent.lock import SingleInstance
from airtap_sync.logs import setup_logging

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1


def lock_path() -> Path:
    return paths.state_dir() / "agent.lock"


def run_agent(config_path: Path, *, gui: bool = True) -> int:
    """Run the background agent until it is quit; returns an exit code."""
    lock = SingleInstance(lock_path())
    if not lock.acquire():
        message = "airtap-sync 后台程序已经在运行"
        print(message, file=sys.stderr) if sys.stderr else log.error(message)
        return EXIT_ERROR
    try:
        ui, use_gui = _choose_ui(gui)
        controller = AgentController(config_path, ui)  # type: ignore[arg-type]
        log.info("agent %s started (gui=%s)", __version__, use_gui)
        if use_gui:
            from airtap_sync.agent.gui import GuiUi

            assert isinstance(ui, GuiUi)
            ui.run(controller)
        else:
            _run_console(controller)
        return EXIT_OK
    finally:
        lock.release()


def _choose_ui(want_gui: bool) -> tuple[Ui, bool]:
    if want_gui:
        from airtap_sync.agent.gui import GuiUi, gui_available

        ok, reason = gui_available()
        if ok:
            return GuiUi(), True
        log.warning("no tray UI (%s); running in text mode", reason)
        if sys.stderr:
            print(f"提示：无法使用托盘界面（{reason}），改为文本模式", file=sys.stderr)
    return ConsoleUi(), False


def _run_console(controller: AgentController) -> None:
    stop = threading.Event()
    controller.ui.set_status(controller.status, "等待 U 盘（Ctrl+C 退出）")  # type: ignore[arg-type]
    try:
        controller.run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
        controller.wait()


def main() -> int:
    """Entry point of the windowless ``airtap-sync-agent`` executable."""
    parser = argparse.ArgumentParser(prog="airtap-sync-agent")
    parser.add_argument("--config", type=Path, help="配置文件路径")
    parser.add_argument("--no-gui", action="store_true", help="不使用托盘界面")
    args = parser.parse_args()
    setup_logging(console=False)
    return run_agent(args.config or paths.default_config_file(), gui=not args.no_gui)


if __name__ == "__main__":
    sys.exit(main())
