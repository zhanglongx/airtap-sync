from __future__ import annotations

from pathlib import Path

import pytest

from airtap_sync.agent.controller import AgentController, Status
from airtap_sync.gitlab.client import GitLabError
from airtap_sync.gitsync.report import GitSyncReport
from airtap_sync.medium.volume import init_volume
from airtap_sync.progress import ProgressEvent, Stage
from airtap_sync.sync import SyncOutcome, SyncResult


class FakeUi:
    def __init__(self) -> None:
        self.statuses: list[tuple[Status, str]] = []
        self.progress_events: list[ProgressEvent] = []
        self.notifications: list[tuple[str, str]] = []
        self.questions: list[tuple[str, str]] = []
        self.answer = True

    def set_status(self, status: Status, text: str) -> None:
        self.statuses.append((status, text))

    def progress(self, event: ProgressEvent) -> None:
        self.progress_events.append(event)

    def notify(self, title: str, message: str) -> None:
        self.notifications.append((title, message))

    def confirm(self, title: str, message: str) -> bool:
        self.questions.append((title, message))
        return self.answer


class Rig:
    """A controller wired to a fake UI, fake drives and a fake sync runner."""

    def __init__(self, tmp_path: Path, auto_sync: bool = True) -> None:
        self.tmp_path = tmp_path
        self.config_path = tmp_path / "config.toml"
        self.write_config(auto_sync)
        self.ui = FakeUi()
        self.mounts: list[Path] = []
        self.calls: list[Path] = []
        self.next_result: SyncResult | Exception = SyncResult("A")
        self.controller = AgentController(
            self.config_path,
            self.ui,
            runner=self._run,
            roots=lambda: list(self.mounts),
        )

    def write_config(self, auto_sync: bool = True) -> None:
        self.config_path.write_text(
            'side = "A"\npair_id = "pair-1"\n'
            f"[agent]\nauto_sync = {str(auto_sync).lower()}\n"
            f'[folders.docs]\npath = "{self.tmp_path}"\n',
            encoding="utf-8",
        )

    def _run(self, config, usb, *, progress, ask_confirmation, **_kw):
        self.calls.append(usb)
        progress(ProgressEvent(Stage.SCAN, "scanning"))
        outcome = self.next_result
        if isinstance(outcome, Exception):
            raise outcome
        if outcome.has_pending_confirmation:
            ask_confirmation(outcome)
        return SyncOutcome(outcome, [self.tmp_path / "report.md"])

    def insert(self, name: str = "usb", pair_id: str = "pair-1") -> Path:
        drive = self.tmp_path / name
        drive.mkdir(exist_ok=True)
        init_volume(drive, pair_id)
        self.mounts.append(drive)
        return drive

    def remove(self, drive: Path) -> None:
        self.mounts.remove(drive)

    def poll(self) -> None:
        self.controller.poll_once()
        self.controller.wait(5)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def attention_result() -> SyncResult:
    return SyncResult("A", git=GitSyncReport("A", conflicts=["p: refs/heads/x"]))


def test_inserted_drive_is_synced_once(rig: Rig) -> None:
    drive = rig.insert()
    rig.poll()
    rig.poll()  # still inserted: no second sync
    assert rig.calls == [drive]
    assert rig.controller.status is Status.OK
    assert [s for s, _ in rig.ui.statuses][:2] == [Status.SYNCING, Status.OK]
    assert rig.ui.notifications[-1][0] == "同步完成，可以拔出 U 盘"
    assert rig.ui.progress_events  # progress reaches the UI
    assert rig.controller.last_report == rig.tmp_path / "report.md"


def test_removing_and_reinserting_syncs_again(rig: Rig) -> None:
    drive = rig.insert()
    rig.poll()
    rig.remove(drive)
    rig.poll()
    assert rig.controller.status is Status.IDLE
    rig.mounts.append(drive)
    rig.poll()
    assert rig.calls == [drive, drive]


def test_drives_of_another_pair_are_ignored(rig: Rig) -> None:
    rig.insert("foreign", pair_id="someone-else")
    rig.poll()
    assert rig.calls == []


def test_attention_outcome(rig: Rig) -> None:
    rig.next_result = attention_result()
    rig.insert()
    rig.poll()
    assert rig.controller.status is Status.ATTENTION
    assert "需要处理" in rig.ui.notifications[-1][0]


def test_sync_errors_are_reported_not_raised(rig: Rig) -> None:
    rig.next_result = GitLabError("令牌失效")
    rig.insert()
    rig.poll()
    assert rig.controller.status is Status.ERROR
    assert rig.ui.notifications[-1] == ("同步失败", "令牌失效")


def test_unexpected_errors_keep_the_agent_alive(rig: Rig) -> None:
    rig.next_result = RuntimeError("boom")
    drive = rig.insert()
    rig.poll()
    assert rig.controller.status is Status.ERROR
    assert "boom" in rig.ui.notifications[-1][1]
    # A failed sync is not retried in a loop while the drive stays in ...
    rig.poll()
    assert rig.calls == [drive]
    # ... but "sync now" works and can succeed.
    rig.next_result = SyncResult("A")
    assert rig.controller.sync_now()
    rig.controller.wait(5)
    assert rig.controller.status is Status.OK


def test_paused_agent_does_not_auto_sync(rig: Rig) -> None:
    rig.controller.pause()
    rig.insert()
    rig.poll()
    assert rig.calls == []
    assert rig.controller.status is Status.PAUSED
    rig.controller.resume()  # a drive inserted during the pause is picked up
    rig.poll()
    assert len(rig.calls) == 1


def test_manual_confirmation_when_auto_sync_is_off(tmp_path: Path) -> None:
    rig = Rig(tmp_path, auto_sync=False)
    rig.ui.answer = False
    rig.insert()
    rig.poll()
    assert rig.calls == []
    assert "发现同步 U 盘" in rig.ui.questions[0][0]

    rig.ui.answer = True
    rig.controller.resume()
    rig.poll()
    assert len(rig.calls) == 1


def test_sync_now_without_a_drive(rig: Rig) -> None:
    assert not rig.controller.sync_now()
    assert rig.ui.notifications[-1][0] == "没有找到同步 U 盘"


def test_only_one_sync_at_a_time(rig: Rig) -> None:
    import threading

    gate = threading.Event()
    original = rig.controller._runner

    def slow(*args, **kwargs):
        gate.wait(5)
        return original(*args, **kwargs)

    rig.controller._runner = slow
    drive = rig.insert()
    assert rig.controller.start_sync(drive)
    assert not rig.controller.start_sync(drive)  # busy
    gate.set()
    rig.controller.wait(5)
    assert len(rig.calls) == 1


def test_confirmation_questions_go_to_the_ui(rig: Rig) -> None:
    result = SyncResult(
        "A", git=GitSyncReport("A", pending_deletes=["p: refs/heads/old"])
    )
    rig.next_result = result
    rig.insert()
    rig.poll()
    title, message = rig.ui.questions[-1]
    assert title == "需要确认" and "p: refs/heads/old" in message


def test_a_broken_config_is_shown_once_and_recovers(rig: Rig) -> None:
    rig.config_path.write_text('side = "Z"\n', encoding="utf-8")
    rig.insert()
    rig.poll()
    rig.poll()
    assert rig.calls == []
    assert [t for t, _ in rig.ui.notifications].count("配置错误") == 1
    assert rig.controller.status is Status.ERROR

    rig.write_config()
    rig.poll()  # the edited config is read without restarting
    assert len(rig.calls) == 1
