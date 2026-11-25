from __future__ import annotations

import json
from pathlib import Path

import pytest

from airtap_sync.medium.outbox import (
    FORMAT_VERSION,
    FormatVersionError,
    OutboxWriter,
    dump_json_gz,
    load_json_gz,
    read_outbox,
    resolve_relpath,
)


def write_outbox(root: Path, side: str = "A") -> OutboxWriter:
    writer = OutboxWriter(root, side)  # type: ignore[arg-type]
    with writer:
        writer.write_manifest("files/docs.json.gz", dump_json_gz({"a.txt": "h1"}))
        writer.write_manifest("git/projects.json.gz", dump_json_gz({"p": []}))
    return writer


def test_roundtrip(tmp_path: Path) -> None:
    writer = write_outbox(tmp_path)
    commit = read_outbox(tmp_path, "A")
    assert commit is not None
    assert commit == writer.commit_info
    assert commit.seq == 1
    assert commit.format_version == FORMAT_VERSION
    assert set(commit.manifests) == {"files/docs.json.gz", "git/projects.json.gz"}
    assert load_json_gz(tmp_path / "files" / "docs.json.gz") == {"a.txt": "h1"}


def test_seq_increments(tmp_path: Path) -> None:
    write_outbox(tmp_path)
    write_outbox(tmp_path)
    commit = read_outbox(tmp_path, "A")
    assert commit is not None
    assert commit.seq == 2


def test_missing_outbox_is_none(tmp_path: Path) -> None:
    assert read_outbox(tmp_path, "A") is None
    assert read_outbox(tmp_path / "nope", "A") is None


def test_interrupted_run_invalidates_outbox(tmp_path: Path) -> None:
    write_outbox(tmp_path)
    writer = OutboxWriter(tmp_path, "A")
    with pytest.raises(RuntimeError), writer:
        writer.write_manifest("files/docs.json.gz", dump_json_gz({"new": 1}))
        raise RuntimeError("drive pulled")
    assert read_outbox(tmp_path, "A") is None


def test_tampered_manifest_is_rejected(tmp_path: Path) -> None:
    write_outbox(tmp_path)
    (tmp_path / "files" / "docs.json.gz").write_bytes(b"garbage")
    assert read_outbox(tmp_path, "A") is None


def test_missing_manifest_is_rejected(tmp_path: Path) -> None:
    write_outbox(tmp_path)
    (tmp_path / "git" / "projects.json.gz").unlink()
    assert read_outbox(tmp_path, "A") is None


def test_corrupt_commit_is_rejected(tmp_path: Path) -> None:
    write_outbox(tmp_path)
    (tmp_path / "commit.json").write_text("{not json", encoding="utf-8")
    assert read_outbox(tmp_path, "A") is None


def test_wrong_side_is_rejected(tmp_path: Path) -> None:
    write_outbox(tmp_path, "A")
    assert read_outbox(tmp_path, "B") is None


def test_format_version_mismatch_raises(tmp_path: Path) -> None:
    write_outbox(tmp_path)
    commit_file = tmp_path / "commit.json"
    doc = json.loads(commit_file.read_text(encoding="utf-8"))
    doc["format_version"] = FORMAT_VERSION + 1
    commit_file.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(FormatVersionError, match="升级"):
        read_outbox(tmp_path, "A")


def test_write_before_begin_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        OutboxWriter(tmp_path, "A").write_manifest("x.json", b"{}")


@pytest.mark.parametrize(
    "rel", ["", "/abs", "../x", "a/../b", "a//b", "a\\b", "C:/x", "./x"]
)
def test_unsafe_paths_rejected(tmp_path: Path, rel: str) -> None:
    with pytest.raises(ValueError):
        resolve_relpath(tmp_path, rel)


def test_json_gz_is_deterministic() -> None:
    assert dump_json_gz({"b": 1, "a": 2}) == dump_json_gz({"a": 2, "b": 1})
