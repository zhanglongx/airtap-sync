"""Build the executables with PyInstaller and zip them for distribution.

    python scripts/build_exe.py

Run it on the platform you are building for (Windows for the .exe files).
The result is ``dist/airtap-sync/`` and ``dist/airtap-sync-<version>-<os>.zip``.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    spec = ROOT / "packaging" / "airtap-sync.spec"
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(spec)],
        cwd=ROOT,
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
