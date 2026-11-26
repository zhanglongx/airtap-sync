"""Loading and validation of the per-machine config.toml.

Unknown keys are rejected so that typos surface immediately instead of being
silently ignored. Error messages are user-facing and therefore in Chinese.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

Side = Literal["A", "B"]
SIDES: tuple[Side, Side] = ("A", "B")

KiB = 1024
MiB = 1024**2
GiB = 1024**3
TiB = 1024**4

_SIZE_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "kib": KiB,
    "mib": MiB,
    "gib": GiB,
    "tib": TiB,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")
_FOLDER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

MAX_CONCURRENCY = 64


def user_path(value: str) -> Path:
    """A configured path with ``~`` and environment variables expanded."""
    return Path(os.path.expandvars(value)).expanduser()


class ConfigError(ValueError):
    """Raised when config.toml is missing, malformed or invalid."""


def parse_size(value: object, *, key: str = "size") -> int:
    """Parse ``28GiB`` / ``512MiB`` / a plain integer into a byte count."""
    if isinstance(value, bool):
        raise ConfigError(f"{key}: 不是有效的大小：{value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"{key}: 大小不能为负数：{value}")
        return value
    if isinstance(value, str):
        match = _SIZE_RE.match(value)
        if match:
            unit = _SIZE_UNITS.get(match.group(2).lower())
            if unit is not None:
                return int(float(match.group(1)) * unit)
    raise ConfigError(f'{key}: 不是有效的大小：{value!r}（示例："512MiB"、"28GiB"）')


@dataclass(frozen=True)
class TransferConfig:
    max_bytes: int = 28 * GiB
    reserve_free: int = 1 * GiB
    part_size: int = 512 * MiB


@dataclass(frozen=True)
class AgentConfig:
    auto_sync: bool = True
    poll_seconds: int = 3


@dataclass(frozen=True)
class FolderConfig:
    name: str
    path: Path
    ignore: tuple[str, ...] = ()


@dataclass(frozen=True)
class GitLabCacheConfig:
    dir: Path
    max_size: int = 100 * GiB


@dataclass(frozen=True)
class GitLabConfig:
    url: str
    cache: GitLabCacheConfig
    include_projects: tuple[str, ...] = ()
    exclude_projects: tuple[str, ...] = ()
    include_branches: tuple[str, ...] = ()
    exclude_branches: tuple[str, ...] = ()
    ci_skip: bool = True
    concurrency: int = 8


@dataclass(frozen=True)
class RelayConfig:
    """A machine that reaches both GitLabs and syncs Git directly (no USB drive)."""

    url_a: str
    url_b: str
    # Everything but the address is shared by both sides (cache, filters, ...).
    template: GitLabConfig

    def gitlab_config(self, side: Side) -> GitLabConfig:
        return replace(self.template, url=self.url_a if side == "A" else self.url_b)


@dataclass(frozen=True)
class SafetyConfig:
    max_deletions: int = 20
    max_new_projects: int = 20


@dataclass(frozen=True)
class Config:
    side: Side
    pair_id: str
    transfer: TransferConfig = field(default_factory=TransferConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    folders: Mapping[str, FolderConfig] = field(default_factory=dict)
    gitlab: GitLabConfig | None = None
    relay: RelayConfig | None = None
    safety: SafetyConfig = field(default_factory=SafetyConfig)

    @property
    def peer_side(self) -> Side:
        return "B" if self.side == "A" else "A"


def load_config(path: Path) -> Config:
    """Read and validate a config file."""
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"找不到配置文件：{path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"配置文件格式错误（{path}）：{e}") from None
    return parse_config(raw)


def parse_config(raw: Mapping[str, Any]) -> Config:
    """Validate an already-parsed TOML document."""
    data = dict(raw)
    # A relay machine has no side and no USB drive of its own.
    relay_only = "relay" in data and "side" not in data and "pair_id" not in data
    side = data.pop("side", "A" if relay_only else None)
    if side not in ("A", "B"):
        raise ConfigError(f'side: 必须是 "A" 或 "B"，当前为 {side!r}')
    pair_id = data.pop("pair_id", "relay" if relay_only else None)
    if not isinstance(pair_id, str) or not pair_id.strip():
        raise ConfigError("pair_id: 必须是非空字符串（与 U 盘 volume.toml 中的一致）")

    config = Config(
        side=side,
        pair_id=pair_id.strip(),
        transfer=_parse_transfer(_pop_table(data, "transfer")),
        agent=_parse_agent(_pop_table(data, "agent")),
        folders=_parse_folders(_pop_table(data, "folders")),
        gitlab=_parse_gitlab(_pop_table(data, "gitlab")) if "gitlab" in data else None,
        relay=_parse_relay(_pop_table(data, "relay")) if "relay" in data else None,
        safety=_parse_safety(_pop_table(data, "safety")),
    )
    _reject_unknown(data, "")
    return config


def _pop_table(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.pop(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}]: 必须是表（table）")
    return dict(value)


def _reject_unknown(table: Mapping[str, Any], section: str) -> None:
    if table:
        names = ", ".join(sorted(table))
        where = f"[{section}] 中" if section else "顶层"
        raise ConfigError(f"{where}有未知的配置项：{names}")


def _pop_bool(table: dict[str, Any], key: str, default: bool, section: str) -> bool:
    value = table.pop(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"[{section}] {key}: 必须是 true 或 false")
    return value


def _pop_int(
    table: dict[str, Any], key: str, default: int, section: str, *, minimum: int = 0
) -> int:
    value = table.pop(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"[{section}] {key}: 必须是不小于 {minimum} 的整数")
    return value


def _pop_str_list(table: dict[str, Any], key: str, section: str) -> tuple[str, ...]:
    value = table.pop(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[{section}] {key}: 必须是非空字符串组成的列表")
    return tuple(value)


def _pop_size(
    table: dict[str, Any], key: str, default: int, section: str, *, positive: bool
) -> int:
    if key not in table:
        return default
    size = parse_size(table.pop(key), key=f"[{section}] {key}")
    if positive and size <= 0:
        raise ConfigError(f"[{section}] {key}: 必须大于 0")
    return size


def _parse_transfer(table: dict[str, Any]) -> TransferConfig:
    defaults = TransferConfig()
    result = TransferConfig(
        max_bytes=_pop_size(
            table, "max_bytes", defaults.max_bytes, "transfer", positive=True
        ),
        reserve_free=_pop_size(
            table, "reserve_free", defaults.reserve_free, "transfer", positive=False
        ),
        part_size=_pop_size(
            table, "part_size", defaults.part_size, "transfer", positive=True
        ),
    )
    _reject_unknown(table, "transfer")
    return result


def _parse_agent(table: dict[str, Any]) -> AgentConfig:
    result = AgentConfig(
        auto_sync=_pop_bool(table, "auto_sync", True, "agent"),
        poll_seconds=_pop_int(table, "poll_seconds", 3, "agent", minimum=1),
    )
    _reject_unknown(table, "agent")
    return result


def _parse_folders(table: dict[str, Any]) -> dict[str, FolderConfig]:
    folders: dict[str, FolderConfig] = {}
    for name, value in table.items():
        section = f"folders.{name}"
        if not _FOLDER_NAME_RE.match(name):
            raise ConfigError(f"[{section}]: 目录名只能包含字母、数字、_ . -")
        if not isinstance(value, dict):
            raise ConfigError(f"[{section}]: 必须是表（table）")
        entry = dict(value)
        path = entry.pop("path", None)
        if not isinstance(path, str) or not path.strip():
            raise ConfigError(f"[{section}] path: 必须是非空字符串")
        folders[name] = FolderConfig(
            name=name,
            path=user_path(path),
            ignore=_pop_str_list(entry, "ignore", section),
        )
        _reject_unknown(entry, section)
    return folders


def _parse_relay(table: dict[str, Any]) -> RelayConfig:
    urls = {}
    for key in ("url_a", "url_b"):
        url = table.pop(key, None)
        if not isinstance(url, str) or not re.match(r"^https?://\S+$", url):
            raise ConfigError(f"[relay] {key}: 必须以 http:// 或 https:// 开头")
        urls[key] = url.rstrip("/")
    if urls["url_a"].lower() == urls["url_b"].lower():
        raise ConfigError("[relay] url_a 和 url_b 不能相同")
    template = _parse_gitlab({**table, "url": urls["url_a"]}, "relay")
    return RelayConfig(urls["url_a"], urls["url_b"], template)


def _parse_gitlab(table: dict[str, Any], section: str = "gitlab") -> GitLabConfig:
    url = table.pop("url", None)
    if not isinstance(url, str) or not re.match(r"^https?://\S+$", url):
        raise ConfigError(f"[{section}] url: 必须以 http:// 或 https:// 开头")

    cache_table = _pop_table(table, "cache")
    cache_dir = cache_table.pop("dir", None)
    if not isinstance(cache_dir, str) or not cache_dir.strip():
        raise ConfigError(f"[{section}.cache] dir: 必须指定缓存目录")
    cache = GitLabCacheConfig(
        dir=user_path(cache_dir),
        max_size=_pop_size(
            cache_table,
            "max_size",
            GitLabCacheConfig.max_size,
            f"{section}.cache",
            positive=True,
        ),
    )
    _reject_unknown(cache_table, f"{section}.cache")

    result = GitLabConfig(
        url=url.rstrip("/"),
        cache=cache,
        include_projects=_pop_str_list(table, "include_projects", section),
        exclude_projects=_pop_str_list(table, "exclude_projects", section),
        include_branches=_pop_str_list(table, "include_branches", section),
        exclude_branches=_pop_str_list(table, "exclude_branches", section),
        ci_skip=_pop_bool(table, "ci_skip", True, section),
        concurrency=_pop_int(table, "concurrency", 8, section, minimum=1),
    )
    if result.concurrency > MAX_CONCURRENCY:
        raise ConfigError(f"[{section}] concurrency: 不能超过 {MAX_CONCURRENCY}")
    _reject_unknown(table, section)
    return result


def _parse_safety(table: dict[str, Any]) -> SafetyConfig:
    defaults = SafetyConfig()
    result = SafetyConfig(
        max_deletions=_pop_int(
            table, "max_deletions", defaults.max_deletions, "safety"
        ),
        max_new_projects=_pop_int(
            table, "max_new_projects", defaults.max_new_projects, "safety"
        ),
    )
    _reject_unknown(table, "safety")
    return result
