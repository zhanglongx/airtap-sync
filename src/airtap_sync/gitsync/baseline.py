"""Initialisation from a GitLab backup/restore: snapshot, baseline and reset.

Before backing up side A, ``take_snapshot`` records every project and its refs.
After the backup has been restored on side B, ``adopt_baseline`` makes B start
from that same record. Both sides then agree on what was shared, so nothing
deleted after the backup is mistaken for a new project or branch.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from airtap_sync.config import Side
from airtap_sync.gitlab.projects import Project
from airtap_sync.gitsync.gitcmd import RefMap
from airtap_sync.gitsync.refs import RefFilter
from airtap_sync.gitsync.scan import RemoteRefs
from airtap_sync.gitsync.state import GitState

BASELINE_VERSION = 1


class BaselineError(Exception):
    """The baseline could not be created or adopted."""


@dataclass
class Baseline:
    side: Side
    created_at: str
    projects: dict[str, tuple[str, RefMap]] = field(default_factory=dict)
    # key -> (path, refs)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": BASELINE_VERSION,
            "side": self.side,
            "created_at": self.created_at,
            "projects": [
                {"path": path, "refs": refs}
                for _, (path, refs) in sorted(self.projects.items())
            ],
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Baseline:
        try:
            if raw["version"] != BASELINE_VERSION:
                raise BaselineError(f"不支持的基准文件版本：{raw['version']}")
            baseline = cls(raw["side"], str(raw["created_at"]))
            for item in raw["projects"]:
                path = str(item["path"])
                refs = {str(k): str(v) for k, v in item["refs"].items()}
                baseline.projects[path.lower()] = (path, refs)
        except (KeyError, TypeError, AttributeError) as e:
            raise BaselineError(f"基准文件格式错误：{e}") from None
        return baseline

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=1), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> Baseline:
        try:
            return cls.from_json(json.loads(path.read_text(encoding="utf-8")))
        except OSError as e:
            raise BaselineError(f"无法读取基准文件：{e}") from None
        except ValueError as e:
            raise BaselineError(f"基准文件不是有效的 JSON：{e}") from None


def take_snapshot(
    side: Side,
    projects: Iterable[Project],
    remote_refs: Mapping[str, RemoteRefs],
    ref_filter: RefFilter,
    state: GitState,
) -> Baseline:
    """Record all projects and refs, and adopt them as this side's own base.

    ``remote_refs`` is keyed by lowercased project path. A project whose refs
    could not be read makes the snapshot invalid: an incomplete record would
    later look like missing branches.
    """
    baseline = Baseline(side, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    failures = []
    for project in projects:
        result = remote_refs.get(project.key)
        if result is None or not result.ok or result.refs is None:
            failures.append(
                f"{project.full_path}: {result.error if result else '未检测'}"
            )
            continue
        baseline.projects[project.key] = (
            project.full_path,
            ref_filter.apply(result.refs),
        )
    if failures:
        shown = "\n  ".join(failures[:10])
        more = f"\n  …另有 {len(failures) - 10} 个" if len(failures) > 10 else ""
        raise BaselineError(
            f"{len(failures)} 个工程读取失败，无法生成快照：\n  {shown}{more}"
        )
    _store(baseline, state)
    return baseline


def _store(baseline: Baseline, state: GitState) -> None:
    state.replace_base(
        {key: path for key, (path, _) in baseline.projects.items()},
        {key: refs for key, (_, refs) in baseline.projects.items()},
    )


@dataclass
class AdoptReport:
    adopted: int = 0
    missing_locally: list[str] = field(default_factory=list)
    refs_differ: list[str] = field(default_factory=list)


def adopt_baseline(
    baseline: Baseline,
    local_projects: Iterable[Project],
    remote_refs: Mapping[str, RemoteRefs],
    ref_filter: RefFilter,
    state: GitState,
) -> AdoptReport:
    """Adopt ``baseline`` as the base, and check it against this GitLab.

    Projects in the baseline that this GitLab lacks are left out of the base
    (the restore is then incomplete) and reported; so are projects whose
    current refs differ from the recorded ones.
    """
    local = {p.key: p for p in local_projects}
    report = AdoptReport()
    shared: dict[str, tuple[str, RefMap]] = {}
    for key, (path, refs) in baseline.projects.items():
        project = local.get(key)
        if project is None:
            report.missing_locally.append(path)
            continue
        refs = ref_filter.apply(refs)
        shared[key] = (project.full_path, refs)
        current = remote_refs.get(key)
        if current and current.ok and current.refs is not None:
            if ref_filter.apply(current.refs) != refs:
                report.refs_differ.append(project.full_path)
    adopted = Baseline(baseline.side, baseline.created_at, shared)
    _store(adopted, state)
    report.adopted = len(shared)
    return report
