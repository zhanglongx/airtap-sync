"""Build the executables with PyInstaller and zip them for distribution.

    python scripts/build_exe.py

Run it on the platform you are building for (Windows for the .exe files).
The result is ``dist/airtap-sync/`` and ``dist/airtap-sync-<version>-<os>.zip``.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for make_icon


# Stale Tcl/Tk variables (e.g. left over from an uninstalled Python) make
# PyInstaller look for Tk data in a directory that no longer exists.
_TK_ENV = ("TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH")


def _tk_works(env: dict[str, str]) -> bool:
    """Whether tkinter can start a Tcl interpreter in a clean environment."""
    probe = subprocess.run(
        [sys.executable, "-c", "import tkinter; tkinter.Tcl()"],
        env=env,
        capture_output=True,
    )
    return probe.returncode == 0


def main() -> int:
    env = {k: v for k, v in os.environ.items() if k not in _TK_ENV}
    dropped = [k for k in _TK_ENV if k in os.environ]
    if dropped:
        print(
            f"提示：构建时忽略环境变量 {', '.join(dropped)}（可能指向已卸载的 Python）"
        )
    if not _tk_works(env):
        # The console program does not need Tk; the agent then runs in text mode.
        print("警告：tkinter 不可用，打包时不包含它（后台程序将只能用文本模式）")
        env["AIRTAP_NO_TK"] = "1"
    if sys.platform == "win32":
        from make_icon import make_ico

        env["AIRTAP_ICON"] = str(make_ico())
    spec = ROOT / "packaging" / "airtap-sync.spec"
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(spec)],
        cwd=ROOT,
        env=env,
        check=True,
    )
    folder = ROOT / "dist" / "airtap-sync"
    name = f"airtap-sync-{version('airtap-sync')}-{platform.system().lower()}"
    archive = shutil.make_archive(
        str(ROOT / "dist" / name), "zip", folder.parent, folder.name
    )
    print(f"程序目录：{folder}")
    print(f"压缩包：{archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
