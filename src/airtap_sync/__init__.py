"""airtap-sync: sync directories and Git repositories between air-gapped networks."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("airtap-sync")
except PackageNotFoundError:  # pragma: no cover - running from an unbuilt tree
    __version__ = "0+unknown"

__all__ = ["__version__"]
