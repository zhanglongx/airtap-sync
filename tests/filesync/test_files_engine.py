from __future__ import annotations

import os
from pathlib import Path

import pytest

from airtap_sync.config import SafetyConfig, TransferConfig
from tests.filesync.harness import FilePair


@pytest.fixture
def pair(tmp_path: Path) -> FilePair:
    return FilePair(tmp_path)


def test_new_files_reach_the_other_side(pair: FilePair) -> None:
    pair["A"].write("a.txt", "alpha")
    pair["A"].write("sub/dir/b.bin", os.urandom(5000))
    first = pair.run("A")
    assert first.folders[0].exported == 0  # handshake: manifest only
    assert any("尚未收到" in w for w in first.folders[0].warnings)

    pair.converge()
    assert pair["B"].files() == pair["A"].files()
    assert pair["B"].read("a.txt") == b"alpha"


def test_mtime_is_preserved(pair: FilePair) -> None:
    src = pair["A"].write("a.txt", "alpha", age_s=86400 * 3)
    pair.converge()
    assert (pair["B"].root / "a.txt").stat().st_mtime_ns == src.stat().st_mtime_ns


def test_changes_and_deletions_propagate_with_trash(pair: FilePair) -> None:
    pair["A"].write("keep.txt", "k")
    pair["A"].write("edit.txt", "v1")
    pair["A"].write("gone.txt", "bye")
    pair.converge()

    pair["A"].write("edit.txt", "version two")
    (pair["A"].root / "gone.txt").unlink()
    pair["B"].write("from-b.txt", "hello A")
    pair.converge()

    assert pair["B"].read("edit.txt") == b"version two"
    assert not pair["B"].exists("gone.txt")
    assert pair["A"].read("from-b.txt") == b"hello A"
    assert pair["A"].files() == pair["B"].files()
    trash = pair["B"].root / ".airtap-sync" / "trash"
    kept = {p.name: p.read_bytes() for p in trash.rglob("*") if p.is_file()}
    assert kept["gone.txt"] == b"bye"  # nothing is lost
    assert kept["edit.txt"] == b"v1"


def test_conflict_keeps_both_versions_on_both_sides(pair: FilePair) -> None:
    pair["A"].write("note.txt", "original")
    pair.converge()
    pair["A"].write("note.txt", "edited on A", age_s=7200)
    pair["B"].write("note.txt", "edited on B", age_s=60)  # newer
    pair.converge()

    assert pair["A"].files() == pair["B"].files()
    assert pair["A"].read("note.txt") == b"edited on B"  # the newer one wins
    copies = [n for n in pair["A"].files() if ".sync-conflict-" in n]
    assert len(copies) == 1
    assert copies[0].startswith("note.sync-conflict-A-") and copies[0].endswith(".txt")
    assert pair["A"].read(copies[0]) == b"edited on A"
    assert copies[0] in pair["B"].files()


def test_deleted_here_modified_there_modification_wins(pair: FilePair) -> None:
    pair["A"].write("other.txt", "stays")
    pair["A"].write("f.txt", "v1")
    pair.converge()
    (pair["A"].root / "f.txt").unlink()
    pair["B"].write("f.txt", "v2")
    pair.converge()
    assert pair["A"].read("f.txt") == b"v2"
    assert pair["B"].read("f.txt") == b"v2"


def test_identical_files_on_both_sides_are_not_transferred(pair: FilePair) -> None:
    """After a manual first copy, nothing needs to travel."""
    for side in "AB":
        pair[side].write("big.bin", b"same content" * 100)
    pair.run("A")
    pair.run("B")
    report = pair.run("A")
    assert report.folders[0].exported == 0
    pair.run("B")
    assert pair["A"].files() == pair["B"].files()


def test_a_renamed_file_moves_without_transferring_content(pair: FilePair) -> None:
    pair["A"].write("old-name.bin", os.urandom(3000))
    pair.converge()
    (pair["A"].root / "old-name.bin").rename(pair["A"].root / "new-name.bin")
    report_a = pair.run("A")
    assert report_a.folders[0].exported == 0  # the peer already has this content
    pair.converge()
    assert pair["B"].exists("new-name.bin") and not pair["B"].exists("old-name.bin")


def test_ignored_files_are_not_synchronised(pair: FilePair) -> None:
    pair["A"].ignore = ("*.log",)
    pair["A"].write("a.txt", "x")
    pair["A"].write("debug.log", "noise")
    pair["A"].write("~$tmp.docx", "lock")
    pair["A"].write(".git/config", "x")
    pair.converge()
    assert set(pair["B"].files()) == {"a.txt"}


def test_ignore_rules_of_both_sides_apply(pair: FilePair) -> None:
    pair["B"].ignore = ("*.bak",)
    pair["A"].write("x.bak", "backup")
    pair["A"].write("x.txt", "text")
    pair.converge()
    assert set(pair["B"].files()) == {"x.txt"}


def test_large_files_are_delivered_in_batches(tmp_path: Path) -> None:
    pair = FilePair(
        tmp_path,
        transfer=TransferConfig(max_bytes=7000, reserve_free=0, part_size=3000),
    )
    blob = os.urandom(20_000)
    pair["A"].write("big.bin", blob)
    waited = False
    for _ in range(14):
        pair.run("A")
        waited |= bool(pair.run("B").folders[0].deferred)
        if pair["B"].exists("big.bin"):
            break
    assert waited
    assert pair["B"].read("big.bin") == blob


def test_several_files_share_a_small_quota(tmp_path: Path) -> None:
    pair = FilePair(
        tmp_path,
        transfer=TransferConfig(max_bytes=5000, reserve_free=0, part_size=4000),
    )
    for i in range(6):
        pair["A"].write(f"f{i}.bin", os.urandom(2500))
    for _ in range(14):
        pair.run("A")
        pair.run("B")
        if pair["A"].files() == pair["B"].files():
            break
    assert pair["A"].files() == pair["B"].files()


def test_blobs_are_cleaned_off_the_drive_once_delivered(pair: FilePair) -> None:
    pair["A"].write("a.bin", os.urandom(2000))
    pair.converge()
    pair.round(1)
    blobs = pair.volume.outbox_dir("A") / "blobs"
    assert not blobs.exists() or not any(p.is_file() for p in blobs.rglob("*"))


def test_missing_root_is_reported_and_nothing_is_published(pair: FilePair) -> None:
    pair["A"].write("a.txt", "x")
    pair.converge()
    import shutil

    shutil.rmtree(pair["A"].root)
    report = pair.run("A")
    assert any("不存在" in f for f in report.folders[0].failures)
    pair.run("B")
    assert pair["B"].exists("a.txt")  # not deleted because A's disk was missing


def test_emptied_folder_is_not_mistaken_for_mass_deletion(pair: FilePair) -> None:
    for i in range(3):
        pair["A"].write(f"f{i}.txt", str(i))
    pair.converge()
    for child in pair["A"].root.iterdir():
        child.unlink()
    report = pair.run("A")
    assert any("空的" in f for f in report.folders[0].failures)
    pair.run("B")
    assert len(pair["B"].files()) == 3


def test_many_deletions_wait_for_confirmation(tmp_path: Path) -> None:
    pair = FilePair(tmp_path, safety=SafetyConfig(max_deletions=2))
    for i in range(5):
        pair["A"].write(f"f{i}.txt", str(i))
    pair.converge()
    for i in range(4):
        (pair["A"].root / f"f{i}.txt").unlink()
    pair.run("A")
    held = pair.run("B")
    assert len(held.folders[0].pending_deletes) == 4
    assert len(pair["B"].files()) == 5
    assert held.has_pending_confirmation

    pair.run("A")
    done = pair.run("B", confirm=True)
    assert done.folders[0].pending_deletes == []
    assert set(pair["B"].files()) == {"f4.txt"}


def test_dry_run_changes_nothing(pair: FilePair) -> None:
    pair["A"].write("a.txt", "x")
    pair.converge()
    pair["A"].write("new.txt", "n")
    pair.round(2)
    pair["A"].write("newer.txt", "n2")
    pair.run("A")
    before = pair["B"].files()
    report = pair.run("B", dry_run=True)
    assert report.dry_run
    assert pair["B"].files() == before


def test_peer_names_that_cannot_be_created_are_reported(pair: FilePair) -> None:
    pair["A"].write("ok.txt", "fine")
    pair.converge()
    from airtap_sync.filesync.manifest import FolderManifest, manifest_path
    from airtap_sync.filesync.scanner import FileEntry
    from airtap_sync.medium.outbox import dump_json_gz, resolve_relpath

    # Forge a peer manifest containing an escaping path.
    path = resolve_relpath(pair.volume.outbox_dir("A"), manifest_path("docs"))
    manifest = FolderManifest(
        "A", "docs", (), {"../evil.txt": FileEntry(1, 1, "0" * 64)}
    )
    path.write_bytes(dump_json_gz(manifest.to_json()))
    commit = pair.volume.outbox_dir("A") / "commit.json"
    commit.unlink()  # (the forged manifest would fail verification anyway)
    report = pair.run("B")
    assert not (pair["B"].root.parent / "evil.txt").exists()
    assert report.folders[0].scanned >= 1


def test_state_survives_between_runs(pair: FilePair) -> None:
    pair["A"].write("a.txt", "one")
    pair.converge()
    base_db = pair["B"].work / "state" / "files" / "docs" / "state.sqlite"
    assert base_db.is_file()
    pair["A"].write("a.txt", "two!")
    pair.converge()
    assert pair["B"].read("a.txt") == b"two!"
