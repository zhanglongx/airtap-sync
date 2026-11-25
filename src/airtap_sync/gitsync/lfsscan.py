"""Find the Git LFS objects referenced by new commits.

An LFS pointer is a tiny text blob::

    version https://git-lfs.github.com/spec/v1
    oid sha256:<64 hex>
    size <bytes>

so only blobs below a small size limit can be pointers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from airtap_sync.gitlab.lfs import LfsObject
from airtap_sync.gitsync.gitcmd import GitRunner

POINTER_LIMIT = 1024  # bytes; real pointers are about 130
_POINTER_RE = re.compile(
    rb"^version https://git-lfs\.github\.com/spec/v1\n"
    rb"oid sha256:(?P<oid>[0-9a-f]{64})\n"
    rb"size (?P<size>\d+)\n?$"
)


def parse_pointer(content: bytes) -> LfsObject | None:
    """The object a pointer blob refers to, or ``None`` if it is not a pointer."""
    match = _POINTER_RE.match(content)
    if match is None:
        return None
    return LfsObject(match.group("oid").decode("ascii"), int(match.group("size")))


def find_lfs_objects(
    runner: GitRunner,
    repo: Path,
    tips: Iterable[str],
    known: Iterable[str] = (),
) -> list[LfsObject]:
    """LFS objects referenced by commits reachable from ``tips`` but not ``known``.

    ``known`` oids that are absent from ``repo`` are ignored.
    """
    tip_list = sorted(set(tips))
    if not tip_list:
        return []
    have = runner.existing_objects(repo, known)
    rev_args = [*tip_list, *(f"^{oid}" for oid in sorted(have))]
    # Blobs over the limit cannot be pointers, so let git leave them out.
    listing = runner.run(
        ["rev-list", "--objects", f"--filter=blob:limit={POINTER_LIMIT}", "--stdin"],
        cwd=repo,
        input_text="\n".join(rev_args) + "\n",
    ).stdout
    oids = []
    for line in listing.splitlines():
        oid = line.split(" ", 1)[0]
        if len(oid) in (40, 64):
            oids.append(oid)
    return _pointers_among(runner, repo, oids)


def _pointers_among(runner: GitRunner, repo: Path, oids: list[str]) -> list[LfsObject]:
    """Read the small blobs among ``oids`` and parse the pointers."""
    if not oids:
        return []
    data = runner.run_binary(
        ["cat-file", "--batch"],
        cwd=repo,
        input_bytes="\n".join(oids).encode("ascii") + b"\n",
    )
    found: dict[str, LfsObject] = {}
    pos = 0
    while pos < len(data):
        end = data.index(b"\n", pos)
        header = data[pos:end].split()
        pos = end + 1
        if len(header) != 3 or header[1] == b"missing":
            continue
        kind, size = header[1], int(header[2])
        content = data[pos : pos + size]
        pos += size + 1  # trailing newline after the content
        if kind == b"blob" and size <= POINTER_LIMIT:
            obj = parse_pointer(content)
            if obj is not None:
                found[obj.oid] = obj
    return sorted(found.values(), key=lambda o: o.oid)
