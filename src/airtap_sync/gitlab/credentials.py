"""Storage of the GitLab access token.

The token lives in the OS keyring (Windows Credential Manager). Where no
keyring backend is available, e.g. a headless Linux box, a JSON file readable
only by the owner is used instead. ``AIRTAP_GITLAB_TOKEN`` overrides both.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Protocol

import keyring
from keyring.backends import fail
from keyring.errors import KeyringError, PasswordDeleteError

from airtap_sync import paths

SERVICE_NAME = "airtap-sync"
ENV_TOKEN = "AIRTAP_GITLAB_TOKEN"


class TokenStore(Protocol):
    def get(self, url: str) -> str | None: ...
    def set(self, url: str, token: str) -> None: ...
    def delete(self, url: str) -> None: ...


class KeyringTokenStore:
    """Tokens in the OS keyring, one entry per GitLab URL."""

    def get(self, url: str) -> str | None:
        return keyring.get_password(SERVICE_NAME, url)

    def set(self, url: str, token: str) -> None:
        keyring.set_password(SERVICE_NAME, url, token)

    def delete(self, url: str) -> None:
        with contextlib.suppress(PasswordDeleteError):
            keyring.delete_password(SERVICE_NAME, url)


class FileTokenStore:
    """Tokens in a JSON file with ``0600`` permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return (
            {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        )

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def get(self, url: str) -> str | None:
        return self._load().get(url)

    def set(self, url: str, token: str) -> None:
        data = self._load()
        data[url] = token
        self._save(data)

    def delete(self, url: str) -> None:
        data = self._load()
        if data.pop(url, None) is not None:
            self._save(data)


def keyring_available() -> bool:
    try:
        return not isinstance(keyring.get_keyring(), fail.Keyring)
    except KeyringError:
        return False


def default_token_store() -> TokenStore:
    if keyring_available():
        return KeyringTokenStore()
    return FileTokenStore(paths.config_dir() / "gitlab-token.json")


def load_token(url: str, store: TokenStore | None = None) -> str | None:
    """Token for ``url`` from the environment, else from the token store."""
    env = os.environ.get(ENV_TOKEN, "").strip()
    if env:
        return env
    return (store or default_token_store()).get(url)
