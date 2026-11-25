"""Pushing ref changes to the local GitLab, with per-ref results."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from airtap_sync.gitsync.gitcmd import GitRunner

log = logging.getLogger(__name__)

_OK_FLAGS = frozenset(" +-*=")


@dataclass
class PushResult:
    ok: set[str] = field(default_factory=set)
    failed: dict[str, str] = field(default_factory=dict)


def parse_porcelain(output: str) -> dict[str, tuple[str, str]]:
    """Map destination ref -> (flag, summary) from ``git push --porcelain``.

    Each ref line is ``<flag>\t<from>:<to>\t<summary>``; ``To <url>`` and
    ``Done`` lines are skipped.
    """
    results: dict[str, tuple[str, str]] = {}
    for line in output.splitlines():
        if line[1:2] != "\t":
            continue
        flag, _, rest = line.partition("\t")
        refspec, _, summary = rest.partition("\t")
        if ":" not in refspec:
            continue
        results[refspec.split(":", 1)[1]] = (flag, summary)
    return results


def push_refs(
    runner: GitRunner,
    repo: Path,
    url: str,
    *,
    updates: Mapping[str, str] | None = None,
    forced: Mapping[str, str] | None = None,
    deletes: Mapping[str, str] | None = None,
    ci_skip: bool = True,
) -> PushResult:
    """Push ref changes and report the outcome of each ref separately.

    ``updates`` are plain (fast-forward or create) pushes; ``forced`` replaces
    refs that airtap-sync itself owns (conflict branches); ``deletes`` maps a
    ref to the value it is expected to have, protecting against a concurrent
    change on the server.

    GitLab's pre-receive hook can decline a whole multi-ref push because of a
    single protected branch. Refs the server explicitly rejected are therefore
    retried one by one, so only the genuinely refused ones end up as failures.
    """
    updates, forced, deletes = updates or {}, forced or {}, deletes or {}
    result = _push_once(runner, repo, url, updates, forced, deletes, ci_skip)
    rejected = [r for r, why in result.failed.items() if "remote rejected" in why]
    if len(rejected) > 0 and len(updates) + len(forced) + len(deletes) > 1:
        for ref in rejected:
            retry = _push_once(
                runner,
                repo,
                url,
                {ref: updates[ref]} if ref in updates else {},
                {ref: forced[ref]} if ref in forced else {},
                {ref: deletes[ref]} if ref in deletes else {},
                ci_skip,
            )
            if ref in retry.ok:
                del result.failed[ref]
                result.ok.add(ref)
            else:
                result.failed[ref] = retry.failed[ref]
    return result


def _push_once(
    runner: GitRunner,
    repo: Path,
    url: str,
    updates: Mapping[str, str],
    forced: Mapping[str, str],
    deletes: Mapping[str, str],
    ci_skip: bool,
) -> PushResult:
    wanted = {*updates, *forced, *deletes}
    result = PushResult()
    if not wanted:
        return result

    def build(with_option: bool) -> list[str]:
        args = ["push", "--porcelain"]
        if with_option and ci_skip:
            args.append("--push-option=ci.skip")
        args += [f"--force-with-lease={ref}:{old}" for ref, old in deletes.items()]
        args.append(url)
        args += [f"{oid}:{ref}" for ref, oid in updates.items()]
        args += [f"+{oid}:{ref}" for ref, oid in forced.items()]
        args += [f":{ref}" for ref in deletes]
        return args

    proc = runner.run(build(True), cwd=repo, check=False)
    if proc.returncode != 0 and "does not support push options" in proc.stderr:
        log.warning("GitLab does not accept push options; pushing without ci.skip")
        proc = runner.run(build(False), cwd=repo, check=False)

    outcomes = parse_porcelain(proc.stdout)
    fallback = proc.stderr.strip() or proc.stdout.strip() or "推送失败"
    for ref in wanted:
        flag, summary = outcomes.get(ref, ("!", fallback))
        if flag in _OK_FLAGS:
            result.ok.add(ref)
        else:
            reason = summary or fallback
            hint = _remote_reason(proc.stderr)
            result.failed[ref] = f"{reason} {hint}".strip()
    return result


def _remote_reason(stderr: str) -> str:
    """Extract GitLab's ``remote:`` explanation (e.g. protected branch)."""
    lines = [
        line.removeprefix("remote:").strip()
        for line in stderr.splitlines()
        if line.startswith("remote:")
    ]
    return " ".join(line for line in lines if line)[:300]
