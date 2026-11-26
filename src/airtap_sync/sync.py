"""One complete sync run: Git and directories around a single outbox commit.

Both parts import first (reading the peer's outbox), then both export into one
``OutboxWriter``, so the peer always sees a consistent pair of manifests, and
share the transfer quota (Git first, directories with what is left).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from airtap_sync import paths
from airtap_sync.config import Config, ConfigError
from airtap_sync.filesync.engine import FilesOptions, FilesSync
from airtap_sync.filesync.report import FilesReport
from airtap_sync.filesync.report import render_markdown as render_files
from airtap_sync.gitlab.client import GitLabError
from airtap_sync.gitsync.context import GitContext, open_context
from airtap_sync.gitsync.engine import GitSync, GitSyncAborted, GitSyncOptions
from airtap_sync.gitsync.gitcmd import GitError
from airtap_sync.gitsync.report import GitSyncReport, body_lines
from airtap_sync.medium.blobs import drop_unwanted_blobs
from airtap_sync.medium.feedback import RECEIVED, Received, load_feedback
from airtap_sync.medium.outbox import FormatVersionError, OutboxWriter, read_outbox
from airtap_sync.medium.quota import usb_budget
from airtap_sync.medium.volume import Volume, VolumeError, open_volume
from airtap_sync.progress import ProgressCallback, null_progress


@dataclass
class SyncOptions:
    git: bool = True
    files: bool = True
    confirm: bool = False
    dry_run: bool = False
    now: datetime | None = None


@dataclass
class SyncResult:
    side: str
    git: GitSyncReport | None = None
    files: FilesReport | None = None
    dry_run: bool = False
    relay: bool = False

    @property
    def needs_attention(self) -> bool:
        return any(r is not None and r.needs_attention for r in (self.git, self.files))

    @property
    def has_pending_confirmation(self) -> bool:
        return any(
            r is not None and r.has_pending_confirmation for r in (self.git, self.files)
        )

    def pending_items(self) -> list[str]:
        items: list[str] = []
        if self.git:
            items += self.git.pending_deletes + self.git.pending_creates
        for folder in self.files.folders if self.files else []:
            items += [f"{folder.name}/{p}" for p in folder.pending_deletes]
        return items

    def summaries(self) -> list[str]:
        out = []
        if self.git:
            out.append(f"Git：{self.git.summary()}")
        if self.files:
            out.append(f"目录：{self.files.summary()}")
        return out


def run_sync(
    config: Config,
    volume: Volume,
    staging: Path,
    state_root: Path,
    options: SyncOptions | None = None,
    git_ctx: GitContext | None = None,
    progress: ProgressCallback = null_progress,
) -> SyncResult:
    """Run the requested parts of a sync for ``config.side`` on ``volume``."""
    options = options or SyncOptions()
    side, peer_side = config.side, config.peer_side
    my_dir, peer_dir = volume.outbox_dir(side), volume.outbox_dir(peer_side)
    peer_commit = read_outbox(peer_dir, peer_side)
    previous = read_outbox(my_dir, side)
    feedback_in = load_feedback(peer_dir, peer_commit)
    feedback_out = Received()

    git = (
        GitSync(
            git_ctx,
            volume,
            side,
            config.transfer,
            staging,
            GitSyncOptions(options.confirm, options.dry_run, options.now),
            progress,
            feedback_out,
        )
        if options.git and git_ctx is not None
        else None
    )
    files = (
        FilesSync(
            config.folders,
            volume,
            side,
            staging,
            state_root,
            config.safety,
            config.transfer.part_size,
            FilesOptions(options.confirm, options.dry_run, options.now),
            progress,
            feedback_out,
        )
        if options.files and config.folders
        else None
    )
    result = SyncResult(side, dry_run=options.dry_run)

    if git is not None:
        git.import_phase(peer_commit, feedback_in)
        result.git = git.report
    if files is not None:
        files.import_phase(peer_commit)
        result.files = files.report
    if options.dry_run:
        if files is not None:
            files.finish()
        return result

    budget = usb_budget(volume.root, config.transfer)
    # A part that did not run keeps the manifests of the previous commit.
    retain = tuple(
        prefix
        for prefix, ran in (("git/", git is not None), ("files/", files is not None))
        if not ran
    )
    with OutboxWriter(my_dir, side, retain=retain) as out:
        if git is not None:
            git.export_phase(out, budget, feedback_in, previous)
        if files is not None:
            files.export_phase(out, budget, feedback_in)
        out.write_manifest(RECEIVED, feedback_out.to_bytes())
        if not retain:  # only a full run knows which blobs are still wanted
            wanted = (git.wanted_blobs if git else set()) | (
                files.wanted_blobs if files else set()
            )
            drop_unwanted_blobs(my_dir, wanted)
    if git is not None:
        git.finish()
    if files is not None:
        files.finish()
    return result


def render_sync_markdown(result: SyncResult, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S UTC")
    title = "同步报告（试运行）" if result.dry_run else "同步报告"
    who = "- 模式：中继（网络 A ⇄ 网络 B）" if result.relay else f"- 端：{result.side}"
    lines = [f"# {title}", "", who, f"- 时间：{stamp}"]
    lines += [f"- {s}" for s in result.summaries()]
    lines.append("")
    if result.git is not None:
        lines += ["## Git 同步", ""]
        lines += body_lines(result.git, level="###")
    if result.files is not None:
        lines += render_files(result.files)
    return "\n".join(lines)


def write_sync_report(
    result: SyncResult, directories: list[Path], now: datetime | None = None
) -> list[Path]:
    """Write the combined report into each directory; return the files written."""
    now = now or datetime.now(UTC)
    who = "relay" if result.relay else result.side
    name = f"{now.strftime('%Y%m%d-%H%M%S')}-{who}.md"
    text = render_sync_markdown(result, now)
    written = []
    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(text, encoding="utf-8")
            written.append(directory / name)
        except OSError:
            continue  # a read-only drive must not fail the whole sync
    return written


def default_state_root() -> Path:
    return paths.state_dir()


class SyncSetupError(Exception):
    """The configuration leaves nothing to synchronise (message is user-facing)."""


# Everything a run can fail with for a reason the user can act on.
SYNC_ERRORS = (
    SyncSetupError,
    ConfigError,
    VolumeError,
    FormatVersionError,
    GitSyncAborted,
    GitError,
    GitLabError,
)


@dataclass
class SyncOutcome:
    result: SyncResult
    report_files: list[Path]

    @property
    def report_file(self) -> Path | None:
        return self.report_files[-1] if self.report_files else None


def choose_parts(config: Config, only: str | None) -> tuple[bool, bool]:
    """Which of Git and directories to run; raises when nothing is left."""
    want_git = only in (None, "git")
    want_files = only in (None, "files")
    if want_git and config.gitlab is None:
        if only == "git":
            raise SyncSetupError("配置中没有 [gitlab]，无法同步 Git")
        want_git = False
    if want_files and not config.folders:
        if only == "files":
            raise SyncSetupError("配置中没有 [folders.*]，没有要同步的目录")
        want_files = False
    if not (want_git or want_files):
        raise SyncSetupError("配置中没有 [gitlab] 或 [folders.*]，没有可同步的内容")
    return want_git, want_files


def execute_sync(
    config: Config,
    usb: Path,
    *,
    only: str | None = None,
    confirm: bool = False,
    dry_run: bool = False,
    progress: ProgressCallback = null_progress,
    ask_confirmation: Callable[[SyncResult], bool] | None = None,
) -> SyncOutcome:
    """Open everything, sync, and write the report (what the CLI and the agent run).

    If deletions or creations were held back by the safety thresholds,
    ``ask_confirmation`` decides whether to run again with them allowed.
    """
    want_git, want_files = choose_parts(config, only)
    ctx = open_context(config) if want_git else None
    try:
        volume = open_volume(usb, config.pair_id)
        options = SyncOptions(
            git=want_git, files=want_files, confirm=confirm, dry_run=dry_run
        )
        while True:
            result = run_sync(
                config,
                volume,
                paths.staging_dir(),
                paths.state_dir(),
                options,
                ctx,
                progress,
            )
            if options.confirm or dry_run or not result.has_pending_confirmation:
                break
            if ask_confirmation is None or not ask_confirmation(result):
                break
            options.confirm = True
    finally:
        if ctx is not None:
            ctx.close()
    directories = [volume.reports_dir, paths.log_dir() / "reports"]
    return SyncOutcome(result, write_sync_report(result, directories))
