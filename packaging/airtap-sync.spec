# PyInstaller spec: two executables sharing one folder (onedir).
#   airtap-sync.exe        console program (sync, init-usb, gitlab login, ...)
#   airtap-sync-agent.exe  windowless background agent with the tray icon
# Build with:  python scripts/build_exe.py
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = Path(SPECPATH).parent

# keyring finds its backends through entry points, so name them explicitly.
hidden = collect_submodules("keyring.backends")
if sys.platform == "win32":
    hidden += ["win32ctypes.pywin32.pywintypes", "win32ctypes.pywin32.win32cred"]
try:
    hidden += collect_submodules("pystray")
except Exception:  # the tray extra is optional
    pass

# __version__ is read with importlib.metadata, which needs the dist-info.
datas = copy_metadata("airtap-sync")

# scripts/build_exe.py sets this when tkinter cannot start in this Python.
excludes = ["tkinter"] if os.environ.get("AIRTAP_NO_TK") else []

common = dict(
    pathex=[str(root / "src")], hiddenimports=hidden, datas=datas, excludes=excludes
)
cli = Analysis([str(root / "packaging" / "entry_cli.py")], **common)
agent = Analysis([str(root / "packaging" / "entry_agent.py")], **common)
MERGE((cli, "entry_cli", "airtap-sync"), (agent, "entry_agent", "airtap-sync-agent"))

cli_exe = EXE(
    PYZ(cli.pure),
    cli.scripts,
    [],
    exclude_binaries=True,
    name="airtap-sync",
    console=True,
)
agent_exe = EXE(
    PYZ(agent.pure),
    agent.scripts,
    [],
    exclude_binaries=True,
    name="airtap-sync-agent",
    console=False,
)
COLLECT(
    cli_exe,
    cli.binaries,
    cli.datas,
    agent_exe,
    agent.binaries,
    agent.datas,
    name="airtap-sync",
)
