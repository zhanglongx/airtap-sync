"""Three-way merge of one folder: what to do with every path.

For each path the inputs are the local content hash ``mine``, the peer's
``theirs`` and the agreed ``base`` (``None`` = absent). Content, not time,
decides whether something changed; time is only used to pick the winner of a
conflict. Nothing is ever lost: the losing version of a conflict is kept next
to the winner, and deleted or overwritten files go to the trash first.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from airtap_sync.config import Side
from airtap_sync.filesync.scanner import FileEntry


class FileActionKind(StrEnum):
    FETCH = "fetch"  # create or replace with the peer's content
    DELETE = "delete"  # the peer deleted it; remove locally (via trash)
    CONFLICT = "conflict"  # both changed; see ``winner``


@dataclass(frozen=True)
class FileAction:
    kind: FileActionKind
    path: str
    local: FileEntry | None = None
    peer: FileEntry | None = None
    # CONFLICT only: which side keeps the canonical name, and where the loser goes.
    winner: str | None = None  # "local" or "peer"
    conflict_path: str | None = None


@dataclass
class FilePlan:
    actions: list[FileAction] = field(default_factory=list)
    # The base once all actions have been applied. Paths the peer has yet to
    # take over from us keep their old base value.
    new_base: dict[str, str] = field(default_factory=dict)


def conflict_name(path: str, loser_side: Side, loser: FileEntry) -> str:
    """Name for the losing version: ``stem.sync-conflict-<side>-<date>-<hash>.ext``.

    Derived only from the loser's own data, so both sides compute the same name
    and the copy converges instead of multiplying.
    """
    directory, name = posixpath.split(path)
    stem, ext = posixpath.splitext(name)
    stamp = datetime.fromtimestamp(loser.mtime_ns / 1e9, UTC).strftime("%Y%m%d")
    new = f"{stem}.sync-conflict-{loser_side}-{stamp}-{loser.sha256[:8]}{ext}"
    return posixpath.join(directory, new) if directory else new


def pick_winner(local: FileEntry, peer: FileEntry) -> str:
    """The newer file wins; equal times fall back to the larger hash (arbitrary
    but identical on both sides)."""
    if local.mtime_ns != peer.mtime_ns:
        return "local" if local.mtime_ns > peer.mtime_ns else "peer"
    return "local" if local.sha256 > peer.sha256 else "peer"


def plan_files(
    local: Mapping[str, FileEntry],
    peer: Mapping[str, FileEntry],
    base: Mapping[str, str],
    *,
    local_side: Side,
    peer_side: Side,
) -> FilePlan:
    plan = FilePlan(new_base=dict(base))
    for path in sorted(set(local) | set(peer) | set(base)):
        mine, theirs = local.get(path), peer.get(path)
        mine_sha = mine.sha256 if mine else None
        theirs_sha = theirs.sha256 if theirs else None
        base_sha = base.get(path)

        action = _decide(path, mine, theirs, base_sha, local_side, peer_side)
        if action is not None:
            plan.actions.append(action)
        agreed = (
            mine_sha == theirs_sha
            or action is not None
            and (
                action.kind is FileActionKind.FETCH
                or action.kind is FileActionKind.DELETE
                or action.winner == "peer"
            )
        )
        if agreed:
            if theirs_sha is None:
                plan.new_base.pop(path, None)
            else:
                plan.new_base[path] = theirs_sha
    return plan


def _decide(
    path: str,
    mine: FileEntry | None,
    theirs: FileEntry | None,
    base_sha: str | None,
    local_side: Side,
    peer_side: Side,
) -> FileAction | None:
    mine_sha = mine.sha256 if mine else None
    theirs_sha = theirs.sha256 if theirs else None
    if mine_sha == theirs_sha:
        return None
    if mine_sha == base_sha:  # only the peer changed
        if theirs is None:
            return FileAction(FileActionKind.DELETE, path, local=mine)
        return FileAction(FileActionKind.FETCH, path, local=mine, peer=theirs)
    if theirs_sha == base_sha:  # only we changed: the peer will take it over
        return None
    # Both changed, and differently.
    if mine is None:  # we deleted it, the peer modified it: modification wins
        return FileAction(FileActionKind.FETCH, path, local=None, peer=theirs)
    if theirs is None:  # the peer deleted it, we modified it: keep ours
        return None
    winner = pick_winner(mine, theirs)
    loser_side, loser = (peer_side, theirs) if winner == "local" else (local_side, mine)
    return FileAction(
        FileActionKind.CONFLICT,
        path,
        local=mine,
        peer=theirs,
        winner=winner,
        conflict_path=conflict_name(path, loser_side, loser),
    )
