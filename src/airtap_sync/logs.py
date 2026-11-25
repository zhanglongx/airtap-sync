"""Logging setup: console plus a rotating file under the platform log dir."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from airtap_sync import paths

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_FILENAME = "airtap-sync.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5


def setup_logging(
    *, verbose: bool = False, log_dir: Path | None = None, console: bool = True
) -> Path:
    """Configure the root logger and return the log file path."""
    directory = log_dir or paths.log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / LOG_FILENAME

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console_handler)
    return log_file
