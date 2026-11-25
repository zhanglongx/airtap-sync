"""Platform-specific locations for configuration, state, logs and staging."""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_config_path, user_data_path, user_log_path

APP_NAME = "airtap-sync"
CONFIG_FILENAME = "config.toml"


def config_dir() -> Path:
    """Directory holding config.toml (%APPDATA%\\airtap-sync on Windows)."""
    return user_config_path(APP_NAME, appauthor=False, roaming=True)


def default_config_file() -> Path:
    return config_dir() / CONFIG_FILENAME


def state_dir() -> Path:
    """Directory for local sync state (bases, hash caches, sqlite files)."""
    return user_data_path(APP_NAME, appauthor=False) / "state"


def staging_dir() -> Path:
    """Directory for incoming file parts and LFS objects awaiting assembly."""
    return user_data_path(APP_NAME, appauthor=False) / "staging"


def log_dir() -> Path:
    return user_log_path(APP_NAME, appauthor=False)
