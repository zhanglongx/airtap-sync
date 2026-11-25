"""Wiring: everything needed to talk to the local GitLab and the repo cache."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from airtap_sync import paths
from airtap_sync.config import Config, ConfigError, GitLabConfig, SafetyConfig
from airtap_sync.gitlab.client import GitLabClient
from airtap_sync.gitlab.credentials import TokenStore, load_token
from airtap_sync.gitlab.projects import (
    Project,
    filter_projects,
    in_scope,
    list_projects,
)
from airtap_sync.gitsync.cache import RepoCache
from airtap_sync.gitsync.gitcmd import GitAuth, GitRunner
from airtap_sync.gitsync.refs import RefFilter
from airtap_sync.gitsync.scan import RemoteRefs, ls_remote_all
from airtap_sync.gitsync.state import GitState


@dataclass
class GitContext:
    config: GitLabConfig
    client: GitLabClient
    runner: GitRunner
    cache: RepoCache
    state: GitState
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    username: str | None = None  # the token's user, once known

    @property
    def max_deletions(self) -> int:
        return self.safety.max_deletions

    def close(self) -> None:
        self.cache.close()
        self.state.close()

    @property
    def ref_filter(self) -> RefFilter:
        return RefFilter(self.config.include_branches, self.config.exclude_branches)

    def discover(self) -> list[Project]:
        """Every project on the local GitLab that is not excluded by config."""
        return filter_projects(
            list_projects(self.client),
            self.config.exclude_projects,
            self.config.include_projects,
        )

    def in_scope(self, full_path: str) -> bool:
        return in_scope(
            full_path, self.config.include_projects, self.config.exclude_projects
        )

    def scan(
        self,
        projects: Iterable[Project],
        on_done: Callable[[RemoteRefs], None] | None = None,
    ) -> dict[str, RemoteRefs]:
        """``git ls-remote`` for all projects, keyed by lowercased path."""
        targets = [(p.key, self.clone_url(p.full_path)) for p in projects]
        return ls_remote_all(
            self.runner, targets, concurrency=self.config.concurrency, on_done=on_done
        )

    def clone_url(self, full_path: str) -> str:
        """URL of a project on the local GitLab, built from the configured base.

        The configured URL is used rather than the one GitLab reports, because
        ``external_url`` on the server may not be reachable from this machine.
        """
        return f"{self.config.url}/{full_path}.git"


def open_context(config: Config, store: TokenStore | None = None) -> GitContext:
    """Create the GitLab client, git runner and cache from the config."""
    gitlab = config.gitlab
    if gitlab is None:
        raise ConfigError("config.toml 中没有 [gitlab] 配置")
    token = load_token(gitlab.url, store)
    if not token:
        raise ConfigError("尚未保存 GitLab 令牌，请先运行：airtap-sync gitlab login")
    runner = GitRunner(GitAuth(gitlab.url, token))
    runner.check_version()
    return GitContext(
        config=gitlab,
        client=GitLabClient(gitlab.url, token),
        runner=runner,
        cache=RepoCache(gitlab.cache.dir, gitlab.cache.max_size, runner),
        state=GitState(paths.state_dir() / "git" / "state.sqlite"),
        safety=config.safety,
    )
