"""Create a multi-size Windows .ico from the application's PNG icon.

    python scripts/make_icon.py [output.ico]

The ``.ico`` is a build artefact (``build/icon.ico`` by default); the source of
truth is ``src/airtap_sync/assets/icon.png``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "airtap_sync" / "assets" / "icon.png"
DEFAULT_OUTPUT = ROOT / "build" / "icon.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def make_ico(source: Path = SOURCE, output: Path = DEFAULT_OUTPUT) -> Path:
    """Write ``output`` with the usual Windows icon sizes and return it."""
    from PIL import Image

    image = Image.open(source).convert("RGBA")
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="ICO", sizes=[(s, s) for s in SIZES])
    return output


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    print(f"图标：{make_ico(output=output)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
