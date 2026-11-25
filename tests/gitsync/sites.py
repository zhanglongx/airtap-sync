"""Two complete sites (fake GitLab + real git repos) syncing through one drive."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from airtap_sync.config import (
    GitLabCacheConfig,
    GitLabConfig,
    SafetyConfig,
    Side,
    TransferConfig,
)
from airtap_sync.gitlab.client import GitLabClient
from airtap_sync.gitsync.cache import RepoCache
from airtap_sync.gitsync.context import GitContext
from airtap_sync.gitsync.engine import GitSyncOptions, run_git_sync
from airtap_sync.gitsync.gitcmd import GitRunner
from airtap_sync.gitsync.report import GitSyncReport
from airtap_sync.gitsync.state import GitState
from airtap_sync.medium.volume import Volume, init_volume
from tests.gitlab.fakegitlab import FakeGitLab
from tests.gitsync.gitrepo import Remote, git


@dataclass
class Site:
    side: Side
    root: Path
    gitlab: FakeGitLab
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    remotes: dict[str, Remote] = field(default_factory=dict)

    def __post_init__(self) -> None:
        runner = GitRunner()
        self.config = GitLabConfig(
            url=self.gitlab.url,
            cache=GitLabCacheConfig(dir=self.root / "cache"),
            concurrency=2,
        )
        self.ctx = GitContext(
            config=self.config,
            client=GitLabClient(self.gitlab.url, "secret"),
            runner=runner,
            cache=RepoCache(self.root / "cache", 10**9, runner),
            state=GitState(self.root / "state.sqlite"),
            safety=self.safety,
        )
        self.ctx.clone_url = lambda path: self.remote(path).url  # type: ignore
        self.next_id = 1

    def remote(self, path: str) -> Remote:
        """The git repo behind ``path``; created on first use (API-created projects)."""
        if path not in self.remotes:
            (self.root / "repos").mkdir(parents=True, exist_ok=True)
            remote = Remote(self.root / "repos", path.replace("/", "__"))
            git("config", "receive.advertisePushOptions", "true", cwd=remote.bare)
            git("config", "receive.denyDeleteCurrent", "ignore", cwd=remote.bare)
            self.remotes[path] = remote
        return self.remotes[path]

    def add_project(self, path: str, **extra) -> Remote:
        remote = self.remote(path)
        self.gitlab.add_project(self.next_id, path, **extra)
        self.next_id += 1
        return remote

    def heads(self, path: str) -> dict[str, str]:
        out = git(
            "for-each-ref",
            "--format=%(objectname) %(refname)",
            cwd=self.remote(path).bare,
        )
        return {
            line.split(" ", 1)[1]: line.split(" ", 1)[0] for line in out.splitlines()
        }

    def project(self, path: str) -> dict | None:
        return next(
            (p for p in self.gitlab.projects if p["path_with_namespace"] == path), None
        )


class Sites:
    def __init__(
        self,
        tmp_path: Path,
        gitlab_a: FakeGitLab,
        gitlab_b: FakeGitLab,
        *,
        transfer: TransferConfig | None = None,
        safety: SafetyConfig | None = None,
    ) -> None:
        (tmp_path / "usb-root").mkdir()
        self.volume: Volume = init_volume(tmp_path / "usb-root", "pair-1")
        self.transfer = transfer or TransferConfig(
            max_bytes=10**9, reserve_free=0, part_size=10**6
        )
        kwargs = {"safety": safety} if safety else {}
        self.sites: dict[str, Site] = {
            "A": Site("A", tmp_path / "A", gitlab_a, **kwargs),
            "B": Site("B", tmp_path / "B", gitlab_b, **kwargs),
        }

    def __getitem__(self, side: str) -> Site:
        return self.sites[side]

    def run(
        self, side: str, *, confirm: bool = False, dry_run: bool = False
    ) -> GitSyncReport:
        site = self.sites[side]
        return run_git_sync(
            site.ctx,
            self.volume,
            site.side,
            self.transfer,
            site.root / "staging",
            GitSyncOptions(confirm=confirm, dry_run=dry_run),
        )

    def round(self) -> None:
        """A, B, A, B: enough for a change on either side to reach the other."""
        for side in "ABAB":
            self.run(side)
