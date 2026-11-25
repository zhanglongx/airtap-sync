from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from airtap_sync.agent import autostart
from airtap_sync.agent.lock import SingleInstance
from airtap_sync.agent.watcher import find_volumes
from airtap_sync.medium.volume import init_volume


def test_find_volumes_matches_by_pair_id(tmp_path: Path) -> None:
    mine, other, plain = (tmp_path / n for n in ("mine", "other", "plain"))
    for d in (mine, other, plain):
        d.mkdir()
    init_volume(mine, "pair-1")
    init_volume(other, "pair-2")
    found = find_volumes("pair-1", lambda: [mine, other, plain, tmp_path / "gone"])
    assert list(found) == [mine]
    assert found[mine].pair_id == "pair-1"


def test_unreadable_marker_is_ignored(tmp_path: Path) -> None:
    drive = tmp_path / "d"
    drive.mkdir()
    volume = init_volume(drive, "p")
    (volume.dir / "volume.toml").write_text("garbage = ", encoding="utf-8")
    assert find_volumes("p", lambda: [drive]) == {}


def test_single_instance(tmp_path: Path) -> None:
    path = tmp_path / "state" / "agent.lock"
    first, second = SingleInstance(path), SingleInstance(path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()
    second.release()  # releasing twice is harmless


def test_autostart_desktop_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(autostart, "_windows", lambda: False)
    assert not autostart.is_enabled()
    assert not autostart.disable()
    where = autostart.enable(["/usr/bin/python3", "-m", "airtap_sync", "agent"])
    entry = Path(where)
    assert entry == tmp_path / "autostart" / "airtap-sync.desktop"
    text = entry.read_text(encoding="utf-8")
    assert "Exec=/usr/bin/python3 -m airtap_sync agent" in text
    assert autostart.is_enabled()
    assert autostart.disable()
    assert not entry.exists()


def test_agent_command_points_at_this_interpreter() -> None:
    command = autostart.agent_command()
    assert command[-2:] == ["airtap_sync", "agent"] or command[-1] == "agent"
    assert command[0].endswith(("python", "python3", "python.exe", "pythonw.exe")) or (
        "python" in command[0].lower()
    )


class FakeRegistry:
    """A stand-in for ``winreg`` keeping values in a dict."""

    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def CreateKey(self, root, path):  # noqa: N802
        return self._key()

    def OpenKey(self, root, path, reserved=0, access=0):  # noqa: N802
        return self._key()

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def _key(self):
        class Key:
            def __enter__(self_inner):  # noqa: N805
                return self_inner

            def __exit__(self_inner, *exc):  # noqa: N805
                return False

        return Key()


def test_autostart_windows_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = FakeRegistry()
    module = types.ModuleType("winreg")
    for name in dir(FakeRegistry):
        if not name.startswith("_"):
            setattr(module, name, getattr(registry, name))
    monkeypatch.setitem(sys.modules, "winreg", module)
    monkeypatch.setattr(autostart, "_windows", lambda: True)

    assert not autostart.is_enabled()
    assert not autostart.disable()
    autostart.enable([r"C:\Program Files\airtap\airtap-sync.exe", "agent"])
    assert (
        registry.values["airtap-sync"]
        == '"C:\\Program Files\\airtap\\airtap-sync.exe" agent'
    )
    assert autostart.is_enabled()
    assert autostart.disable()
    assert not autostart.is_enabled()


def test_frozen_build_prefers_the_windowless_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = tmp_path / "airtap-sync.exe"
    cli.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(cli))
    assert autostart.agent_command() == [str(cli), "agent"]  # no agent next to it
    agent = tmp_path / "airtap-sync-agent.exe"
    agent.write_bytes(b"")
    assert autostart.agent_command() == [str(agent)]
