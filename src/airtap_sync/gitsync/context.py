"""Wiring: everything needed to talk to the local GitLab and the repo cache."""

from __future__ import annotations

from dataclasses import dataclass

from airtap_sync.config import Config, ConfigError, GitLabConfig
from airtap_sync.gitlab.client import GitLabClient
from airtap_sync.gitlab.credentials import TokenStore, load_token
from airtap_sync.gitsync.cache import RepoCache
from airtap_sync.gitsync.gitcmd import GitAuth, GitRunner


@dataclass
class GitContext:
    config: GitLabConfig
    client: GitLabClient
    runner: GitRunner
    cache: RepoCache

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
    )
