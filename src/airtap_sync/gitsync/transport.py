"""Where Git sync data lives inside an outbox, and the receiver's feedback."""

from __future__ import annotations

from pathlib import Path

from airtap_sync.gitsync.cache import repo_key

GIT_MANIFEST = "git/projects.json.gz"


def bundle_base(outbox_dir: Path, project_path: str, sha256: str) -> Path:
    """Base path of a project's bundle (parts append ``.partNNNN``)."""
    return outbox_dir / "git" / "bundles" / repo_key(project_path) / f"{sha256}.bundle"


def bundle_dir(outbox_dir: Path, project_path: str) -> Path:
    return outbox_dir / "git" / "bundles" / repo_key(project_path)
