from pathlib import Path

from airtap_sync.config import load_config


def test_example_config_is_valid() -> None:
    config = load_config(Path(__file__).parents[1] / "config.example.toml")
    assert config.side == "A"
    assert config.gitlab is not None
    assert config.gitlab.url == "http://gitlab-a.example.com"
    assert config.safety.max_deletions == 20


def test_relay_example_config_is_valid() -> None:
    config = load_config(Path(__file__).parents[1] / "config-relay.example.toml")
    assert config.relay is not None
    assert config.relay.url_a == "http://gitlab-a.example.com"
    assert config.relay.url_b == "http://gitlab-b.example.com"
    assert str(config.relay.template.cache.dir).endswith("airtap-relay-cache")
    assert config.safety.max_deletions == 20
    assert config.gitlab is None and not config.folders  # relay-only machine
