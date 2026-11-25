"""Start the agent at login: registry Run key (Windows) or an autostart entry."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

APP_KEY = "airtap-sync"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _windows() -> bool:
    return os.name == "nt"


def agent_command() -> list[str]:
    """The command line that starts the agent from this installation."""
    if getattr(sys, "frozen", False):  # a PyInstaller build
        return [sys.executable, "agent"]
    exe = Path(sys.executable)
    if _windows():  # no console window
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            exe = windowless
    return [str(exe), "-m", "airtap_sync", "agent"]


def _desktop_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / f"{APP_KEY}.desktop"


def _desktop_entry(command: list[str]) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=airtap-sync\n"
        "Comment=Sync directories and Git repositories via USB drive\n"
        f"Exec={shlex.join(command)}\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _windows_command(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)


def enable(command: list[str] | None = None) -> str:
    """Enable autostart; return where it was registered."""
    command = command or agent_command()
    if _windows():
        import winreg  # type: ignore[import-not-found]

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
            winreg.SetValueEx(key, APP_KEY, 0, winreg.REG_SZ, _windows_command(command))
        return f"HKCU\\{WINDOWS_RUN_KEY}\\{APP_KEY}"
    path = _desktop_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_desktop_entry(command), encoding="utf-8")
    return str(path)


def disable() -> bool:
    """Disable autostart; ``False`` if it was not enabled."""
    if _windows():
        import winreg  # type: ignore[import-not-found]

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, APP_KEY)
        except FileNotFoundError:
            return False
        return True
    path = _desktop_file()
    if not path.exists():
        return False
    path.unlink()
    return True


def is_enabled() -> bool:
    if _windows():
        import winreg  # type: ignore[import-not-found]

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
                winreg.QueryValueEx(key, APP_KEY)
        except FileNotFoundError:
            return False
        return True
    return _desktop_file().exists()
