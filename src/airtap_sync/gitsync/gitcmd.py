"""Thin wrapper around the system ``git`` executable.

Credentials never appear in URLs, command lines or logs: they are injected via
``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n`` environment
variables as an ``http.extraHeader`` scoped to the GitLab URL only.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MIN_GIT_VERSION = (2, 31)  # GIT_CONFIG_COUNT support
LOCAL_HEADS = "refs/local/heads"
LOCAL_TAGS = "refs/local/tags"
FETCH_REFSPECS = (
    f"+refs/heads/*:{LOCAL_HEADS}/*",
    f"+refs/tags/*:{LOCAL_TAGS}/*",
)

RefMap = dict[str, str]  # full ref name -> object id


def _decode(data: bytes) -> str:
    """Decode git output like text mode would: UTF-8, universal newlines."""
    return data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


class GitError(Exception):
    """A git command failed."""

    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass(frozen=True)
class GitAuth:
    """HTTP Basic credentials for one GitLab base URL."""

    base_url: str
    token: str
    username: str = "airtap-sync"

    def entries(self) -> list[tuple[str, str]]:
        """The ``git config`` settings (key, value) that authenticate this URL."""
        basic = base64.b64encode(f"{self.username}:{self.token}".encode()).decode()
        scope = f"http.{self.base_url.rstrip('/')}/"
        return [
            (f"{scope}.extraHeader", f"Authorization: Basic {basic}"),
            # GitLab is on the internal network; bypass any configured proxy.
            (f"{scope}.proxy", ""),
        ]

    def env(self) -> dict[str, str]:
        return CombinedAuth([self]).env()


@dataclass(frozen=True)
class CombinedAuth:
    """Credentials for several GitLabs at once (the relay talks to two)."""

    auths: Sequence[GitAuth]

    def env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        entries = [e for auth in self.auths for e in auth.entries()]
        for i, (key, value) in enumerate(entries):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
        env["GIT_CONFIG_COUNT"] = str(len(entries))
        return env


class GitRunner:
    """Runs git commands with a fixed environment."""

    def __init__(
        self, auth: GitAuth | CombinedAuth | None = None, git: str = "git"
    ) -> None:
        self.git = git
        self.auth = auth

    def _env(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env["LC_ALL"] = "C"
        if self.auth is not None:
            env.update(self.auth.env())
        if extra:
            env.update(extra)
        return env

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.git, *args]
        log.debug("git %s", " ".join(args))
        # Bytes in and out: text mode would turn the "\n" of ``input_text`` into
        # "\r\n" on Windows, which git's line-based stdin commands reject.
        try:
            raw = subprocess.run(
                cmd,
                cwd=cwd,
                env=self._env(extra_env),
                capture_output=True,
                input=None if input_text is None else input_text.encode("utf-8"),
            )
        except FileNotFoundError:
            raise GitError("找不到 git，请先安装 Git 并加入 PATH") from None
        proc = subprocess.CompletedProcess(
            raw.args, raw.returncode, _decode(raw.stdout), _decode(raw.stderr)
        )
        if check and proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise GitError(
                f"git {args[0]} 失败（退出码 {proc.returncode}）：{detail}",
                proc.returncode,
            )
        return proc

    def run_binary(
        self, args: Sequence[str], *, cwd: Path | None = None, input_bytes: bytes = b""
    ) -> bytes:
        """Run git and return raw stdout (for commands that stream object content)."""
        try:
            proc = subprocess.run(
                [self.git, *args],
                cwd=cwd,
                env=self._env(None),
                input=input_bytes,
                capture_output=True,
            )
        except FileNotFoundError:
            raise GitError("找不到 git，请先安装 Git 并加入 PATH") from None
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git {args[0]} 失败（退出码 {proc.returncode}）：{detail}")
        return proc.stdout

    def version(self) -> tuple[int, ...]:
        out = self.run(["--version"]).stdout
        match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
        if not match:
            raise GitError(f"无法识别 git 版本：{out.strip()}")
        return tuple(int(g) for g in match.groups() if g is not None)

    def check_version(self) -> None:
        version = self.version()
        if version[:2] < MIN_GIT_VERSION:
            need = ".".join(map(str, MIN_GIT_VERSION))
            have = ".".join(map(str, version))
            raise GitError(f"git 版本过低（{have}），至少需要 {need}")

    # ----- remote operations -------------------------------------------------

    def ls_remote(self, url: str) -> RefMap:
        """Branches and tags of a remote; an empty repository yields ``{}``."""
        out = self.run(["ls-remote", "--heads", "--tags", url]).stdout
        return parse_ls_remote(out)

    def init_bare(self, repo: Path) -> None:
        repo.mkdir(parents=True, exist_ok=True)
        # An empty template keeps the sample hooks out of every cached repo.
        self.run(["init", "--bare", "--quiet", "--template=", str(repo)])
        self.run(["config", "gc.auto", "0"], cwd=repo)

    def fetch_local(self, repo: Path, url: str) -> None:
        """Mirror the remote's branches and tags into ``refs/local/*``."""
        self.run(
            ["fetch", "--quiet", "--no-tags", "--prune", url, *FETCH_REFSPECS],
            cwd=repo,
        )

    # ----- local operations --------------------------------------------------

    def list_refs(self, repo: Path, prefix: str) -> RefMap:
        out = self.run(
            ["for-each-ref", "--format=%(objectname) %(refname)", prefix], cwd=repo
        ).stdout
        refs: RefMap = {}
        for line in out.splitlines():
            oid, _, name = line.partition(" ")
            refs[name] = oid
        return refs

    def existing_objects(self, repo: Path, oids: Iterable[str]) -> set[str]:
        """The subset of ``oids`` that exist in ``repo``."""
        wanted = sorted(set(oids))
        if not wanted:
            return set()
        out = self.run(
            ["cat-file", "--batch-check"], cwd=repo, input_text="\n".join(wanted) + "\n"
        ).stdout
        present = set()
        for line in out.splitlines():
            oid, _, rest = line.partition(" ")
            if not rest.startswith("missing"):
                present.add(oid)
        return present

    def is_ancestor(self, repo: Path, ancestor: str, descendant: str) -> bool:
        """Whether ``ancestor`` is reachable from ``descendant`` (or equal)."""
        proc = self.run(
            ["merge-base", "--is-ancestor", ancestor, descendant], cwd=repo, check=False
        )
        if proc.returncode in (0, 1):
            return proc.returncode == 0
        raise GitError(f"git merge-base 失败：{proc.stderr.strip()}", proc.returncode)

    def update_refs(self, repo: Path, updates: Mapping[str, str | None]) -> None:
        """Set (or, for ``None``, delete) several refs in one transaction."""
        if not updates:
            return
        lines = ["start"]
        for ref, oid in updates.items():
            lines.append(f"delete {ref}" if oid is None else f"update {ref} {oid}")
        lines += ["prepare", "commit"]
        self.run(
            ["update-ref", "--stdin"], cwd=repo, input_text="\n".join(lines) + "\n"
        )


def parse_ls_remote(output: str) -> RefMap:
    """Parse ``git ls-remote`` output, dropping peeled ``^{}`` entries."""
    refs: RefMap = {}
    for line in output.splitlines():
        oid, _, name = line.partition("\t")
        if not name or name.endswith("^{}"):
            continue
        refs[name] = oid
    return refs
