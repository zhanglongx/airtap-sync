"""Creating, verifying and importing git bundles.

A bundle carries the objects the peer lacks. The sender excludes everything the
peer is known to have (the peer's last reported refs and the agreed base) as
prerequisites; if the peer turns out to be missing one of them, verification
fails with ``MissingPrerequisites`` and the sender falls back to a full bundle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from airtap_sync.gitsync.gitcmd import GitError, GitRunner, RefMap

LOCAL_PREFIX = "refs/local/"
PEER_PREFIX = "refs/peer/"
PEER_REFSPECS = (
    f"+{LOCAL_PREFIX}heads/*:{PEER_PREFIX}heads/*",
    f"+{LOCAL_PREFIX}tags/*:{PEER_PREFIX}tags/*",
)
_OID_RE = re.compile(r"\b[0-9a-f]{40}\b|\b[0-9a-f]{64}\b")


class BundleError(GitError):
    """A bundle could not be created, verified or imported."""


class MissingPrerequisites(BundleError):
    """The repository lacks commits the bundle is based on."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"缺少 bundle 所依赖的 {len(missing)} 个提交")
        self.missing = missing


def create_bundle(
    runner: GitRunner,
    repo: Path,
    out: Path,
    refs: Iterable[str],
    known_oids: Iterable[str] = (),
) -> bool:
    """Write a bundle of ``refs`` minus everything reachable from ``known_oids``.

    ``known_oids`` that this repository does not have are ignored. Returns
    ``False`` (and writes nothing) if the peer already has everything.
    """
    ref_list = sorted(set(refs))
    if not ref_list:
        return False
    known = runner.existing_objects(repo, known_oids)
    rev_args = [*ref_list, *(f"^{oid}" for oid in sorted(known))]
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = runner.run(
        ["bundle", "create", "--quiet", str(out), "--stdin"],
        cwd=repo,
        check=False,
        input_text="\n".join(rev_args) + "\n",
    )
    if proc.returncode == 0:
        return True
    if "empty bundle" in proc.stderr:
        out.unlink(missing_ok=True)
        return False
    out.unlink(missing_ok=True)
    raise BundleError(f"git bundle create 失败：{proc.stderr.strip()}", proc.returncode)


def verify_bundle(runner: GitRunner, repo: Path, bundle: Path) -> None:
    """Check ``bundle`` is intact and applicable to ``repo``."""
    proc = runner.run(["bundle", "verify", str(bundle)], cwd=repo, check=False)
    if proc.returncode == 0:
        return
    if "prerequisite" in proc.stderr:
        missing_part = proc.stderr.split("prerequisite", 1)[1]
        raise MissingPrerequisites(_OID_RE.findall(missing_part))
    raise BundleError(f"bundle 校验失败：{proc.stderr.strip()}", proc.returncode)


def import_bundle(runner: GitRunner, repo: Path, bundle: Path) -> RefMap:
    """Fetch a verified bundle into ``refs/peer/*``; return those refs."""
    verify_bundle(runner, repo, bundle)
    runner.run(["fetch", "--quiet", "--no-tags", str(bundle), *PEER_REFSPECS], cwd=repo)
    return runner.list_refs(repo, PEER_PREFIX.rstrip("/"))
