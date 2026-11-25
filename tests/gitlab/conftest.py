from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.gitlab.fakegitlab import FakeGitLab


@pytest.fixture
def fake_gitlab() -> Iterator[FakeGitLab]:
    server = FakeGitLab().start()
    try:
        yield server
    finally:
        server.stop()
