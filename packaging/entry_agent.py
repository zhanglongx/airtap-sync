"""PyInstaller entry point of the windowless agent (``airtap-sync-agent``)."""

import sys

from airtap_sync.agent.app import main

if __name__ == "__main__":
    sys.exit(main())
