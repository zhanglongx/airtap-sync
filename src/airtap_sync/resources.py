"""Bundled resources (the application icon)."""

from __future__ import annotations

from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def icon_png() -> Path | None:
    """The application icon (512x512 PNG), or ``None`` if it is not shipped."""
    path = ASSETS_DIR / "icon.png"
    return path if path.is_file() else None
