"""The project manifest each side publishes: every project and its refs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from airtap_sync.config import Side
from airtap_sync.gitlab.projects import Project
from airtap_sync.gitsync.gitcmd import RefMap

MANIFEST_VERSION = 1


@dataclass(frozen=True)
class ProjectRecord:
    """One project as seen by the side that publishes it."""

    path: str
    project_id: int
    name: str
    description: str
    visibility: str
    default_branch: str | None
    archived: bool
    lfs_enabled: bool
    namespace_kind: str  # "user" or "group"
    namespace_path: str
    # Branches and tags (after filtering); ``None`` if they could not be read,
    # in which case the peer must leave the project alone this round.
    refs: RefMap | None = None
    renamed_from: str | None = None

    @property
    def key(self) -> str:
        return self.path.lower()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProjectRecord:
        refs = raw.get("refs")
        return cls(
            path=str(raw["path"]),
            project_id=int(raw["project_id"]),
            name=str(raw["name"]),
            description=str(raw.get("description") or ""),
            visibility=str(raw.get("visibility") or "private"),
            default_branch=raw.get("default_branch"),
            archived=bool(raw.get("archived", False)),
            lfs_enabled=bool(raw.get("lfs_enabled", False)),
            namespace_kind=str(raw.get("namespace_kind", "group")),
            namespace_path=str(raw.get("namespace_path", "")),
            refs=None if refs is None else {str(k): str(v) for k, v in refs.items()},
            renamed_from=raw.get("renamed_from"),
        )


def record_from_project(
    project: Project, refs: RefMap | None, renamed_from: str | None = None
) -> ProjectRecord:
    return ProjectRecord(
        path=project.full_path,
        project_id=project.id,
        name=project.name,
        description=project.description,
        visibility=project.visibility,
        default_branch=project.default_branch,
        archived=project.archived,
        lfs_enabled=project.lfs_enabled,
        namespace_kind=project.namespace_kind,
        namespace_path=project.namespace_path,
        refs=refs,
        renamed_from=renamed_from,
    )


@dataclass(frozen=True)
class BundleInfo:
    """A bundle the publishing side placed in its outbox for one project."""

    sha256: str
    size: int
    full: bool  # carries all history (no prerequisites)
    # Hash of the inputs it was built from; an unchanged signature means the
    # existing bundle (and any parts already delivered) can be reused as is.
    signature: str
    # LFS objects ``(oid, size)`` referenced by the commits this bundle adds;
    # the receiver must upload them to its GitLab before pushing the refs.
    lfs: tuple[tuple[str, int], ...] = ()


@dataclass
class ProjectsManifest:
    side: Side
    projects: dict[str, ProjectRecord] = field(default_factory=dict)  # by key
    bundles: dict[str, BundleInfo] = field(default_factory=dict)  # by key

    @classmethod
    def of(cls, side: Side, records: Iterable[ProjectRecord]) -> ProjectsManifest:
        return cls(side, {r.key: r for r in records})

    def to_json(self) -> dict[str, Any]:
        return {
            "version": MANIFEST_VERSION,
            "side": self.side,
            "projects": [r.to_dict() for _, r in sorted(self.projects.items())],
            "bundles": {k: asdict(b) for k, b in sorted(self.bundles.items())},
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> ProjectsManifest:
        if raw.get("version") != MANIFEST_VERSION:
            raise ValueError(f"unsupported manifest version {raw.get('version')!r}")
        side = raw["side"]
        if side not in ("A", "B"):
            raise ValueError(f"bad side {side!r}")
        manifest = cls.of(side, (ProjectRecord.from_dict(p) for p in raw["projects"]))
        for key, b in raw.get("bundles", {}).items():
            manifest.bundles[str(key)] = BundleInfo(
                sha256=str(b["sha256"]),
                size=int(b["size"]),
                full=bool(b["full"]),
                signature=str(b["signature"]),
                lfs=tuple((str(o), int(s)) for o, s in b.get("lfs", [])),
            )
        return manifest
