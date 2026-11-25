"""PyInstaller entry point of the console executable (``airtap-sync``)."""

import sys

from airtap_sync.cli import main

if __name__ == "__main__":
    sys.exit(main())
