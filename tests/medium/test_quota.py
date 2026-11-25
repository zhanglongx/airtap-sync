from __future__ import annotations

from pathlib import Path

import pytest

from airtap_sync.config import GiB, TransferConfig
from airtap_sync.medium import quota
from airtap_sync.medium.quota import Budget, compute_budget, usb_budget


@pytest.mark.parametrize(
    ("max_bytes", "reserve", "free", "expected"),
    [
        (28 * GiB, 1 * GiB, 100 * GiB, 28 * GiB),
        (28 * GiB, 1 * GiB, 10 * GiB, 9 * GiB),
        (28 * GiB, 1 * GiB, 1 * GiB, 0),
        (28 * GiB, 1 * GiB, 0, 0),
    ],
)
def test_compute_budget(max_bytes: int, reserve: int, free: int, expected: int) -> None:
    assert compute_budget(max_bytes, reserve, free) == expected


def test_budget_consumption() -> None:
    budget = Budget(10)
    assert budget.try_consume(4)
    assert not budget.try_consume(7)
    assert budget.remaining == 6
    assert budget.try_consume(6)
    assert (budget.remaining, budget.spent) == (0, 10)


def test_usb_budget_uses_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Usage:
        free = 5 * GiB

    monkeypatch.setattr(quota.shutil, "disk_usage", lambda _p: Usage)
    budget = usb_budget(tmp_path, TransferConfig(max_bytes=28 * GiB, reserve_free=GiB))
    assert budget.remaining == 4 * GiB
