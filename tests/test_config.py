from __future__ import annotations

from pathlib import Path

import pytest

from airtap_sync.config import (
    ConfigError,
    GiB,
    MiB,
    load_config,
    parse_config,
    parse_size,
)

FULL = """
side = "A"
pair_id = "abc-123"

[transfer]
max_bytes = "20GiB"
reserve_free = "2GiB"
part_size = "256MiB"

[agent]
auto_sync = false

[folders.docs]
path = '/data/docs'
ignore = ["build/", "*.log"]

[gitlab]
url = "http://gitlab-a.example/"
exclude_projects = ["sandbox/*"]
exclude_branches = ["wip/*"]
ci_skip = false
concurrency = 4

[gitlab.cache]
dir = "/cache"
max_size = "50GiB"

[safety]
max_deletions = 5
max_new_projects = 3
"""

MINIMAL = {"side": "B", "pair_id": "p"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("512MiB", 512 * MiB),
        ("28GiB", 28 * GiB),
        ("1.5GiB", int(1.5 * GiB)),
        ("100 gib", 100 * GiB),
        ("2GB", 2 * 1000**3),
        ("1024", 1024),
        (4096, 4096),
    ],
)
def test_parse_size(text: str | int, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "GiB", "12XB", "-1", -1, True, 1.5, None])
def test_parse_size_rejects_invalid(bad: object) -> None:
    with pytest.raises(ConfigError):
        parse_size(bad)


def test_load_full_config(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text(FULL, encoding="utf-8")
    config = load_config(file)

    assert config.side == "A"
    assert config.peer_side == "B"
    assert config.pair_id == "abc-123"
    assert config.transfer.max_bytes == 20 * GiB
    assert config.transfer.part_size == 256 * MiB
    assert config.agent.auto_sync is False
    assert config.folders["docs"].path == Path("/data/docs")
    assert config.folders["docs"].ignore == ("build/", "*.log")
    assert config.gitlab is not None
    assert config.gitlab.url == "http://gitlab-a.example"
    assert config.gitlab.cache.max_size == 50 * GiB
    assert config.gitlab.exclude_branches == ("wip/*",)
    assert config.gitlab.ci_skip is False
    assert config.gitlab.concurrency == 4
    assert config.safety.max_deletions == 5
    assert config.safety.max_new_projects == 3


def test_defaults() -> None:
    config = parse_config(MINIMAL)
    assert config.transfer.max_bytes == 28 * GiB
    assert config.transfer.reserve_free == 1 * GiB
    assert config.transfer.part_size == 512 * MiB
    assert config.agent.auto_sync is True
    assert config.folders == {}
    assert config.gitlab is None
    assert config.safety.max_deletions == 20
    assert config.safety.max_new_projects == 20


def test_gitlab_defaults() -> None:
    config = parse_config(
        {**MINIMAL, "gitlab": {"url": "https://g.example", "cache": {"dir": "/c"}}}
    )
    assert config.gitlab is not None
    assert config.gitlab.cache.max_size == 100 * GiB
    assert config.gitlab.ci_skip is True
    assert config.gitlab.concurrency == 8
    assert config.gitlab.include_branches == ()


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"pair_id": "p"}, "side"),
        ({"side": "C", "pair_id": "p"}, "side"),
        ({"side": "A"}, "pair_id"),
        ({"side": "A", "pair_id": "  "}, "pair_id"),
        ({**MINIMAL, "sidee": 1}, "未知"),
        ({**MINIMAL, "transfer": {"part_sise": "1MiB"}}, "transfer"),
        ({**MINIMAL, "transfer": {"part_size": "0"}}, "part_size"),
        ({**MINIMAL, "transfer": {"max_bytes": "abc"}}, "max_bytes"),
        ({**MINIMAL, "transfer": "x"}, "transfer"),
        ({**MINIMAL, "agent": {"auto_sync": "yes"}}, "auto_sync"),
        ({**MINIMAL, "folders": {"bad name": {"path": "/x"}}}, "目录名"),
        ({**MINIMAL, "folders": {"docs": {}}}, "path"),
        ({**MINIMAL, "folders": {"docs": {"path": "/x", "ignore": "*.log"}}}, "ignore"),
        ({**MINIMAL, "gitlab": {"url": "ftp://x", "cache": {"dir": "/c"}}}, "url"),
        ({**MINIMAL, "gitlab": {"url": "http://x"}}, "cache"),
        (
            {
                **MINIMAL,
                "gitlab": {"url": "http://x", "cache": {"dir": "/c"}, "concurrency": 0},
            },
            "concurrency",
        ),
        (
            {
                **MINIMAL,
                "gitlab": {
                    "url": "http://x",
                    "cache": {"dir": "/c"},
                    "concurrency": 1000,
                },
            },
            "concurrency",
        ),
        ({**MINIMAL, "safety": {"max_deletions": -1}}, "max_deletions"),
    ],
)
def test_invalid_config(raw: dict, fragment: str) -> None:
    with pytest.raises(ConfigError, match=fragment):
        parse_config(raw)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="找不到"):
        load_config(tmp_path / "nope.toml")


def test_malformed_toml(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text("side = ", encoding="utf-8")
    with pytest.raises(ConfigError, match="格式错误"):
        load_config(file)
