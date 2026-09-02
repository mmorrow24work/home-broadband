import pytest
import yaml

from collector.config import ConfigError, load_config

BASE = {
    "publish": {"enabled": False},
    "latency": {
        "targets": [
            {"name": "Router", "host": "192.168.1.1", "group": "lan", "checks": ["icmp"]},
            {"name": "CF", "host": "1.1.1.1", "checks": ["icmp"], "primary": True},
        ]
    },
}


def write(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_defaults_are_merged_in(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    assert cfg.get("latency.count") == 5
    assert cfg.get("speed.engines") == ["ookla", "cloudflare"]
    assert cfg.primary_target["name"] == "CF"


def test_user_values_beat_defaults_without_dropping_siblings(tmp_path):
    data = {**BASE, "latency": {**BASE["latency"], "count": 20}}
    cfg = load_config(write(tmp_path, data))
    assert cfg.get("latency.count") == 20
    assert cfg.get("latency.timeout_seconds") == 6  # default survives the merge


def test_targets_are_filtered_by_check(tmp_path):
    data = {
        **BASE,
        "latency": {
            "targets": [
                {"name": "A", "host": "1.1.1.1", "checks": ["icmp"]},
                {"name": "B", "host": "bbc.co.uk", "checks": ["dns", "http"],
                 "url": "https://bbc.co.uk"},
            ]
        },
    }
    cfg = load_config(write(tmp_path, data))
    assert [t["name"] for t in cfg.targets_with("icmp")] == ["A"]
    assert [t["name"] for t in cfg.targets_with("http")] == ["B"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"latency": {"targets": []}}, "empty"),
        (
            {"latency": {"targets": [{"name": "A", "host": "1.1.1.1", "checks": ["ping"]}]}},
            "unknown checks",
        ),
        (
            {"latency": {"targets": [{"name": "A", "host": "x", "checks": ["http"]}]}},
            "url' is required",
        ),
        (
            {"latency": {"targets": [{"name": "A", "host": "1.1.1.1", "family": "ipv7"}]}},
            "family must be one of",
        ),
        (
            {
                "latency": {
                    "targets": [
                        {"name": "A", "host": "1.1.1.1"},
                        {"name": "A", "host": "8.8.8.8"},
                    ]
                }
            },
            "duplicate target name",
        ),
        ({"speed": {"engines": ["iperf"]}}, "unknown engine"),
        ({"publish": {"enabled": True, "remote": ""}}, "publish.remote is empty"),
    ],
)
def test_bad_configs_fail_loudly(tmp_path, mutation, message):
    data = {**BASE, **mutation}
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, data))


def test_two_primaries_rejected(tmp_path):
    data = {
        **BASE,
        "latency": {
            "targets": [
                {"name": "A", "host": "1.1.1.1", "primary": True},
                {"name": "B", "host": "8.8.8.8", "primary": True},
            ]
        },
    }
    with pytest.raises(ConfigError, match="only one target"):
        load_config(write(tmp_path, data))


def test_example_config_is_valid():
    """The shipped example must always load — it is what install.sh seeds."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "config" / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.targets
    assert cfg.primary_target is not None


def test_three_engines_are_valid(tmp_path):
    data = {**BASE, "speed": {"engines": ["ookla", "speedtest-cli", "cloudflare"]}}
    cfg = load_config(write(tmp_path, data))
    assert cfg.get("speed.engines") == ["ookla", "speedtest-cli", "cloudflare"]
    # each engine keeps its own settings block
    assert cfg.get("speed.speedtest-cli.binary") == "speedtest-cli"
    assert cfg.get("speed.ookla.binary") == "speedtest"


def test_empty_engine_list_is_rejected(tmp_path):
    data = {**BASE, "speed": {"engines": []}}
    with pytest.raises(ConfigError, match="speed.engines is empty"):
        load_config(write(tmp_path, data))


def test_duplicate_engines_are_rejected(tmp_path):
    """Listing one twice would silently double its share of the rotation."""
    data = {**BASE, "speed": {"engines": ["cloudflare", "cloudflare"]}}
    with pytest.raises(ConfigError, match="only be listed once"):
        load_config(write(tmp_path, data))


def test_every_valid_engine_has_a_runner():
    """Config validation and the dispatch table must not drift apart."""
    from collector.config import VALID_ENGINES
    from collector.main import SPEED_ENGINES

    assert set(SPEED_ENGINES) == VALID_ENGINES


@pytest.mark.parametrize(
    "host",
    [
        "[www.bbc.co.uk](https://www.bbc.co.uk)",   # markdown link from a paste
        "https://www.bbc.co.uk/",                    # a URL, not a host
        "www.bbc.co.uk ",                            # trailing space
        "one two",
    ],
)
def test_hosts_that_are_paste_accidents_are_rejected(tmp_path, host):
    """These resolve to nothing and would sit red forever if allowed through."""
    data = {**BASE, "latency": {"targets": [{"name": "A", "host": host}]}}
    with pytest.raises(ConfigError, match="not a hostname or IP address"):
        load_config(write(tmp_path, data))


@pytest.mark.parametrize(
    "host",
    ["www.bbc.co.uk", "192.168.1.5", "2606:4700:4700::1111", "api.anthropic.com",
     "fe80::1%eth0", "my-host.local"],
)
def test_real_hosts_are_accepted(tmp_path, host):
    data = {**BASE, "latency": {"targets": [{"name": "A", "host": host}]}}
    assert load_config(write(tmp_path, data)).targets[0]["host"] == host
