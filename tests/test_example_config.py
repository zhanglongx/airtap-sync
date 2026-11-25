from pathlib import Path

from airtap_sync.config import load_config


def test_example_config_is_valid() -> None:
    config = load_config(Path(__file__).parents[1] / "config.example.toml")
    assert config.side == "A"
    assert config.gitlab is not None
    assert config.gitlab.url == "http://gitlab-a.example.com"
    assert config.safety.max_deletions == 20
