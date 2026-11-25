from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from airtap_sync.medium.parts import (
    PartsError,
    PartsIndex,
    build_index,
    fetch,
    index_path,
    part_path,
    present_parts,
    read_index,
    remove_parts,
    store,
)
from airtap_sync.medium.quota import Budget

PART = 100


def make_file(path: Path, size: int) -> bytes:
    data = os.urandom(size)
    path.write_bytes(data)
    return data


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_build_index(tmp_path: Path) -> None:
    src = tmp_path / "src"
    data = make_file(src, 250)
    index = build_index(src, PART)
    assert index.sha256 == sha(data)
    assert index.size == 250
    assert index.count == 3
    assert [index.length(i) for i in range(3)] == [100, 100, 50]
    assert index.part_hashes[1] == sha(data[100:200])
    assert PartsIndex.from_json(index.to_json()) == index


def test_build_index_exact_multiple(tmp_path: Path) -> None:
    src = tmp_path / "src"
    make_file(src, 200)
    assert build_index(src, PART).count == 2


def test_small_file_stored_whole(tmp_path: Path) -> None:
    src = tmp_path / "src"
    data = make_file(src, 80)
    base = tmp_path / "usb" / "blob"
    budget = Budget(1000)
    result = store(src, base, part_size=PART, budget=budget)
    assert result.complete
    assert base.read_bytes() == data
    assert budget.spent == 80
    assert not index_path(base).exists()

    # Storing again costs nothing.
    again = store(src, base, part_size=PART, budget=budget)
    assert again.complete
    assert budget.spent == 80


def test_small_file_over_budget(tmp_path: Path) -> None:
    src = tmp_path / "src"
    make_file(src, 80)
    result = store(src, tmp_path / "blob", part_size=PART, budget=Budget(79))
    assert not result.complete
    assert not (tmp_path / "blob").exists()


def test_roundtrip_in_one_run(tmp_path: Path) -> None:
    src = tmp_path / "src"
    data = make_file(src, 250)
    base = tmp_path / "usb" / "blob"
    result = store(src, base, part_size=PART)
    assert result.complete
    assert result.parts_written == (0, 1, 2)
    assert read_index(base) is not None

    dest = tmp_path / "out" / "file"
    fetched = fetch(
        base, tmp_path / "staging" / "blob", dest, sha256=sha(data), size=250
    )
    assert fetched.complete
    assert dest.read_bytes() == data
    assert not list((tmp_path / "staging").glob("blob.part*"))


def test_batched_transfer_over_several_runs(tmp_path: Path) -> None:
    src = tmp_path / "src"
    data = make_file(src, 250)
    base = tmp_path / "usb" / "blob"
    staging = tmp_path / "staging" / "blob"
    dest = tmp_path / "out" / "file"
    kwargs = {"sha256": sha(data), "size": 250}

    # Day 1: budget fits two parts only.
    r1 = store(src, base, part_size=PART, budget=Budget(200))
    assert not r1.complete
    assert r1.parts_written == (0, 1)
    f1 = fetch(base, staging, dest, **kwargs)
    assert not f1.complete
    assert f1.staged == {0, 1}
    assert not dest.exists()

    # Day 2: receiver reports parts 0 and 1; sender writes only the rest.
    r2 = store(src, base, part_size=PART, budget=Budget(1000), skip=f1.staged)
    assert r2.complete
    assert r2.parts_written == (2,)
    f2 = fetch(base, staging, dest, **kwargs)
    assert f2.complete
    assert dest.read_bytes() == data


def test_sender_does_not_recharge_parts_already_on_drive(tmp_path: Path) -> None:
    src = tmp_path / "src"
    make_file(src, 250)
    base = tmp_path / "usb" / "blob"
    store(src, base, part_size=PART, budget=Budget(200))
    budget = Budget(1000)
    result = store(src, base, part_size=PART, budget=budget)
    assert result.parts_written == (2,)
    assert budget.spent == 50


def test_changed_source_discards_old_parts(tmp_path: Path) -> None:
    src = tmp_path / "src"
    make_file(src, 250)
    base = tmp_path / "usb" / "blob"
    store(src, base, part_size=PART, budget=Budget(200))
    data = make_file(src, 250)  # new content, same size
    result = store(src, base, part_size=PART)
    assert result.complete
    assert result.parts_written == (0, 1, 2)
    dest = tmp_path / "out"
    assert fetch(
        base, tmp_path / "st" / "blob", dest, sha256=sha(data), size=250
    ).complete
    assert dest.read_bytes() == data


def test_corrupt_part_detected(tmp_path: Path) -> None:
    src = tmp_path / "src"
    data = make_file(src, 250)
    base = tmp_path / "usb" / "blob"
    store(src, base, part_size=PART)
    part = part_path(base, 1)
    part.write_bytes(b"x" * 100)
    with pytest.raises(PartsError, match="corrupted"):
        fetch(
            base, tmp_path / "st" / "blob", tmp_path / "o", sha256=sha(data), size=250
        )


def test_truncated_part_is_treated_as_missing(tmp_path: Path) -> None:
    src = tmp_path / "src"
    data = make_file(src, 250)
    base = tmp_path / "usb" / "blob"
    store(src, base, part_size=PART)
    part_path(base, 2).write_bytes(b"short")
    index = read_index(base)
    assert index is not None
    assert present_parts(base, index) == {0, 1}
    res = fetch(
        base, tmp_path / "st" / "blob", tmp_path / "o", sha256=sha(data), size=250
    )
    assert not res.complete
    assert res.staged == {0, 1}


def test_fetch_whole_file_verifies_content(tmp_path: Path) -> None:
    src = tmp_path / "src"
    data = make_file(src, 50)
    base = tmp_path / "usb" / "blob"
    store(src, base, part_size=PART)
    dest = tmp_path / "out"
    assert fetch(base, tmp_path / "st", dest, sha256=sha(data), size=50).complete
    with pytest.raises(PartsError):
        fetch(base, tmp_path / "st", tmp_path / "bad", sha256="0" * 64, size=50)
    assert not (tmp_path / "bad").exists()


def test_fetch_missing_source(tmp_path: Path) -> None:
    res = fetch(tmp_path / "none", tmp_path / "st", tmp_path / "o", sha256="x", size=1)
    assert not res.complete


def test_fetch_rejects_index_mismatch(tmp_path: Path) -> None:
    src = tmp_path / "src"
    make_file(src, 250)
    base = tmp_path / "usb" / "blob"
    store(src, base, part_size=PART)
    with pytest.raises(PartsError, match="manifest"):
        fetch(base, tmp_path / "st", tmp_path / "o", sha256="0" * 64, size=250)


def test_source_changed_during_split(tmp_path: Path, monkeypatch) -> None:
    from airtap_sync.medium import parts

    src = tmp_path / "src"
    make_file(src, 250)
    real = parts.build_index

    def stale_index(path: Path, part_size: int) -> PartsIndex:
        index = real(path, part_size)
        make_file(path, 250)  # file rewritten after hashing
        return index

    monkeypatch.setattr(parts, "build_index", stale_index)
    with pytest.raises(PartsError, match="changed"):
        store(src, tmp_path / "usb" / "blob", part_size=PART)


def test_remove_parts(tmp_path: Path) -> None:
    src = tmp_path / "src"
    make_file(src, 250)
    base = tmp_path / "usb" / "blob"
    store(src, base, part_size=PART)
    remove_parts(base)
    assert list(base.parent.iterdir()) == []


def test_malformed_index_rejected() -> None:
    with pytest.raises(PartsError):
        PartsIndex.from_json(b"{}")
    bad = PartsIndex("h", 250, 100, ("a", "b"))  # needs 3 parts
    with pytest.raises(PartsError, match="inconsistent"):
        PartsIndex.from_json(bad.to_json())
