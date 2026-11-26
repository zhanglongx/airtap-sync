"""Turning one project's sync result into report lines."""

from __future__ import annotations

from airtap_sync.gitsync.projectsync import ProjectSyncResult
from airtap_sync.gitsync.refs import ActionKind
from airtap_sync.gitsync.report import GitSyncReport


def record_result(report: GitSyncReport, result: ProjectSyncResult) -> bool:
    """Add ``result`` to ``report``; ``False`` if the project failed outright."""
    if result.error:
        report.failures.append(f"{result.path}：{result.error}")
        return False
    report.warnings += [f"{result.path}：{n}" for n in result.notes]
    if result.created:
        report.created.append(result.path)
    refs = result.refs
    assert refs is not None
    if refs.applied or result.created:
        report.updated.append(result.path)
    report.refs_applied += len(refs.applied)
    report.refs_deleted += [
        f"{result.path}: {a.ref}" for a in refs.applied if a.kind is ActionKind.DELETE
    ]
    report.refs_deleted += [f"{result.path}: {r}" for r in refs.cleaned]
    report.conflicts += [f"{result.path}: {a.ref}（{a.note}）" for a in refs.conflicts]
    report.deferred += [f"{result.path}: {a.ref}（{a.note}）" for a in refs.deferred]
    report.pending_deletes += [f"{result.path}: {a.ref}" for a in refs.pending_deletes]
    report.failures += [
        f"{result.path}: {ref}：{why}" for ref, why in refs.failed.items()
    ]
    return True
