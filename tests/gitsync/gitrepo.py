"""Helpers that build real git repositories for tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args: str, cwd: Path) -> str:
    env = {**os.environ, **IDENTITY, "LC_ALL": "C"}
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


class Remote:
    """A bare repository plus a work clone used to create commits in it."""

    def __init__(self, root: Path, name: str) -> None:
        self.bare = root / f"{name}.git"
        self.work = root / f"{name}-work"
        git("init", "--bare", "-b", "main", str(self.bare), cwd=root)
        git("clone", "--quiet", str(self.bare), str(self.work), cwd=root)
        git("checkout", "-q", "-b", "main", cwd=self.work)

    @property
    def url(self) -> str:
        return self.bare.as_uri()

    def commit(self, filename: str, content: str, branch: str = "main") -> str:
        """Commit a file on ``branch`` and push.

        The work clone is first brought up to date with the bare repo, since the
        sync under test changes the bare repo behind the clone's back. A new
        branch starts from the current HEAD.
        """
        git("fetch", "-q", "--prune", "origin", cwd=self.work)
        if git("branch", "-r", "--list", f"origin/{branch}", cwd=self.work):
            git("checkout", "-q", "-B", branch, f"origin/{branch}", cwd=self.work)
        else:
            git("checkout", "-q", "-B", branch, cwd=self.work)
        (self.work / filename).write_text(content, encoding="utf-8")
        git("add", filename, cwd=self.work)
        git("commit", "-q", "-m", f"{filename}: {content}", cwd=self.work)
        git("push", "-q", "origin", branch, cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def tag(self, name: str, *, annotated: bool = False) -> None:
        if annotated:
            git("tag", "-a", name, "-m", name, cwd=self.work)
        else:
            git("tag", name, cwd=self.work)
        git("push", "-q", "origin", name, cwd=self.work)

    def delete_branch(self, branch: str) -> None:
        git("push", "-q", "origin", "--delete", branch, cwd=self.work)

    def rev(self, ref: str) -> str:
        return git("rev-parse", ref, cwd=self.bare)
