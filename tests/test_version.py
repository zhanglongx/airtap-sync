import re
import tomllib
from pathlib import Path

import airtap_sync


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "version"
    ]
    assert airtap_sync.__version__ == expected
    assert re.match(r"^\d+\.\d+\.\d+", expected)
