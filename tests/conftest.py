from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from airtap_sync import paths
from tests.gitlab.fakegitlab import FakeGitLab


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests from touching the real user config, state and log dirs."""
    monkeypatch.setattr(paths, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(paths, "staging_dir", lambda: tmp_path / "staging")


@pytest.fixture
def fake_gitlab() -> Iterator[FakeGitLab]:
    server = FakeGitLab().start()
    try:
        yield server
    finally:
        server.stop()
