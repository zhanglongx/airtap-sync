"""Remove build artefacts and caches (what ``make clean`` runs; no shell needed).

python scripts/clean.py          # build output and caches
python scripts/clean.py --all    # also the virtual environment
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTEFACTS = ["build", "dist", ".pytest_cache", ".ruff_cache", ".mypy_cache"]
EVERYTHING = [".venv"]


def clean(*, everything: bool = False, root: Path = ROOT) -> list[Path]:
    """Delete the artefacts under ``root``; return what was removed."""
    removed: list[Path] = []
    targets = [root / name for name in ARTEFACTS + (EVERYTHING if everything else [])]
    targets += [p for p in (root / "src").glob("*.egg-info")]
    targets += [p for p in root.rglob("__pycache__") if ".venv" not in p.parts]
    for target in targets:
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(target)
    return removed


def main() -> int:
    removed = clean(everything="--all" in sys.argv[1:])
    for path in removed:
        print(f"已删除 {path.relative_to(ROOT)}")
    if not removed:
        print("没有需要清理的内容")
    return 0


if __name__ == "__main__":
    sys.exit(main())
