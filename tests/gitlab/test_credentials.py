from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from airtap_sync.gitlab import credentials
from airtap_sync.gitlab.credentials import (
    ENV_TOKEN,
    FileTokenStore,
    default_token_store,
    load_token,
)


def test_file_store_roundtrip(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "sub" / "tok.json")
    assert store.get("http://a") is None
    store.set("http://a", "t1")
    store.set("http://b", "t2")
    assert store.get("http://a") == "t1"
    assert store.get("http://b") == "t2"
    store.delete("http://a")
    assert store.get("http://a") is None
    assert store.get("http://b") == "t2"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_file_store_is_private(tmp_path: Path) -> None:
    path = tmp_path / "tok.json"
    FileTokenStore(path).set("http://a", "t")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_file_store_tolerates_garbage(tmp_path: Path) -> None:
    path = tmp_path / "tok.json"
    path.write_text("not json", encoding="utf-8")
    store = FileTokenStore(path)
    assert store.get("http://a") is None
    store.set("http://a", "t")
    assert store.get("http://a") == "t"


def test_env_overrides_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FileTokenStore(tmp_path / "tok.json")
    store.set("http://a", "stored")
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    assert load_token("http://a", store) == "stored"
    monkeypatch.setenv(ENV_TOKEN, " from-env ")
    assert load_token("http://a", store) == "from-env"


def test_default_store_falls_back_to_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credentials, "keyring_available", lambda: False)
    assert isinstance(default_token_store(), FileTokenStore)
    monkeypatch.setattr(credentials, "keyring_available", lambda: True)
    assert isinstance(default_token_store(), credentials.KeyringTokenStore)
