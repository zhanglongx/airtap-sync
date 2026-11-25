from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from airtap_sync.gitlab.projects import parse_project
from airtap_sync.gitsync.manifest import (
    ProjectRecord,
    ProjectsManifest,
    record_from_project,
)
from airtap_sync.gitsync.projectplan import (
    ProjectAction,
    detect_renames,
    plan_projects,
)
from airtap_sync.gitsync.state import GitState


def record(
    path: str,
    *,
    refs: dict | None = None,
    renamed_from: str | None = None,
    pid: int = 1,
) -> ProjectRecord:
    return ProjectRecord(
        path=path,
        project_id=pid,
        name=path.rsplit("/", 1)[-1],
        description="d",
        visibility="private",
        default_branch="main",
        archived=False,
        lfs_enabled=True,
        namespace_kind="group",
        namespace_path=path.rsplit("/", 1)[0],
        refs={} if refs is None else refs,
        renamed_from=renamed_from,
    )


def recs(*items: ProjectRecord) -> dict[str, ProjectRecord]:
    return {r.key: r for r in items}


# ----- manifest ----------------------------------------------------------------


def test_manifest_roundtrip() -> None:
    manifest = ProjectsManifest.of(
        "A",
        [
            record("Team/App", refs={"refs/heads/main": "a" * 40}, renamed_from="old"),
            ProjectRecord(**{**record("t/unknown").to_dict(), "refs": None}),
        ],
    )
    restored = ProjectsManifest.from_json(manifest.to_json())
    assert restored == manifest
    assert restored.projects["team/app"].renamed_from == "old"
    assert restored.projects["t/unknown"].refs is None


def test_manifest_rejects_other_versions() -> None:
    doc = ProjectsManifest.of("A", []).to_json()
    doc["version"] = 99
    with pytest.raises(ValueError, match="version"):
        ProjectsManifest.from_json(doc)


def test_record_from_project() -> None:
    project = parse_project(
        {
            "id": 5,
            "path": "app",
            "path_with_namespace": "team/app",
            "name": "App",
            "visibility": "internal",
            "default_branch": "dev",
            "archived": True,
            "lfs_enabled": True,
            "namespace": {"id": 2, "kind": "group", "full_path": "team"},
        }
    )
    rec = record_from_project(project, {"refs/heads/dev": "1" * 40}, "team/old")
    assert (rec.path, rec.name, rec.visibility) == ("team/app", "App", "internal")
    assert rec.archived and rec.lfs_enabled
    assert (rec.namespace_kind, rec.namespace_path) == ("group", "team")
    assert rec.renamed_from == "team/old"


# ----- state ---------------------------------------------------------------------


def test_state_roundtrip(tmp_path: Path) -> None:
    state = GitState(tmp_path / "s" / "state.sqlite")
    assert state.base_projects() == {}
    state.set_base_projects({"a/x": "A/x", "a/y": "a/y"})
    state.set_base_refs("a/x", {"refs/heads/main": "1"})
    state.set_peer_refs("a/x", {"refs/heads/main": "2"})
    assert state.base_refs("a/x") == {"refs/heads/main": "1"}
    assert state.peer_refs("a/x") == {"refs/heads/main": "2"}
    assert state.base_refs("a/y") == {}
    state.set_base_refs("a/x", {"refs/heads/dev": "3"})  # replaces
    assert state.base_refs("a/x") == {"refs/heads/dev": "3"}

    state.set_base_projects({"a/y": "a/y"})  # x leaves the base -> its refs go
    assert state.base_refs("a/x") == {}

    state.set_local_ids({1: "a/y", 2: "b/z"})
    assert state.local_ids() == {1: "a/y", 2: "b/z"}
    state.close()

    again = GitState(tmp_path / "s" / "state.sqlite")  # persisted
    assert again.base_projects() == {"a/y": "a/y"}
    again.forget_project("a/y")
    assert again.base_projects() == {}
    again.set_base_projects({"k": "K"})
    again.clear()
    assert again.base_projects() == {} and again.local_ids() == {}


def test_replace_base_ignores_refs_of_unlisted_projects(tmp_path: Path) -> None:
    state = GitState(tmp_path / "state.sqlite")
    state.replace_base({"a": "A"}, {"a": {"r": "1"}, "ghost": {"r": "2"}})
    assert state.base_projects() == {"a": "A"}
    assert state.base_refs("a") == {"r": "1"}
    assert state.base_refs("ghost") == {}


# ----- project plan -----------------------------------------------------------------


def actions(plan) -> dict[str, ProjectAction]:
    return {d.key: d.action for d in plan.decisions}


def test_shared_project_is_synced() -> None:
    both = recs(record("t/a"))
    plan = plan_projects(both, both, set())
    assert actions(plan) == {"t/a": ProjectAction.SYNC}
    assert plan.shared == {"t/a": "t/a"}


def test_unknown_refs_skip_sync() -> None:
    unknown = replace(record("t/a"), refs=None)
    plan = plan_projects(recs(unknown), recs(record("t/a")), set())
    assert actions(plan) == {"t/a": ProjectAction.SKIP}
    assert plan.shared == {"t/a": "t/a"}


def test_new_peer_project_is_created_locally() -> None:
    plan = plan_projects({}, recs(record("t/new")), set())
    assert actions(plan) == {"t/new": ProjectAction.CREATE_LOCAL}
    assert plan.shared == {}


def test_new_local_project_waits_for_peer() -> None:
    plan = plan_projects(recs(record("t/new")), {}, set())
    assert actions(plan) == {"t/new": ProjectAction.LOCAL_NEW}


def test_vanished_projects_are_reported_not_recreated() -> None:
    plan = plan_projects({}, recs(record("t/a")), {"t/a"})
    assert actions(plan) == {"t/a": ProjectAction.GONE_LOCAL}
    plan = plan_projects(recs(record("t/a")), {}, {"t/a"})
    assert actions(plan) == {"t/a": ProjectAction.GONE_PEER}


def test_gone_on_both_sides_disappears() -> None:
    assert plan_projects({}, {}, {"t/a"}).decisions == []


def test_peer_rename_is_reported_instead_of_creating_a_duplicate() -> None:
    local = recs(record("t/old"))
    peer = recs(record("t/new", renamed_from="t/old"))
    plan = plan_projects(local, peer, {"t/old"})
    assert actions(plan) == {
        "t/old": ProjectAction.GONE_PEER,
        "t/new": ProjectAction.RENAMED_PEER,
    }
    (decision,) = plan.of(ProjectAction.RENAMED_PEER)
    assert "t/old" in decision.note and "t/new" in decision.note


def test_local_rename_is_reported() -> None:
    local = recs(record("t/new", renamed_from="t/old"))
    peer = recs(record("t/old"))
    plan = plan_projects(local, peer, {"t/old"})
    assert actions(plan)["t/new"] is ProjectAction.RENAMED_LOCAL
    assert actions(plan)["t/old"] is ProjectAction.GONE_LOCAL


def test_rename_followed_by_manual_alignment_syncs_again() -> None:
    both = recs(record("t/new", renamed_from="t/old"))
    plan = plan_projects(both, recs(record("t/new")), {"t/old"})
    assert actions(plan) == {"t/new": ProjectAction.SYNC}


def test_case_insensitive_matching() -> None:
    plan = plan_projects(recs(record("Team/App")), recs(record("team/app")), set())
    assert actions(plan) == {"team/app": ProjectAction.SYNC}


def test_detect_renames() -> None:
    previous = {1: "t/old", 2: "t/same", 3: "T/Case"}
    current = {1: "t/new", 2: "t/same", 3: "t/case", 4: "t/fresh"}
    assert detect_renames(current, previous) == {"t/new": "t/old"}
