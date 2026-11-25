"""Skip measuring when a travelling collector is not on the home network."""

import argparse

import pytest

from collector import link, main
from collector.config import ConfigError, validate
from tests.test_publish import make_config


@pytest.mark.parametrize("gateway,mac,home,expected", [
    ("192.168.1.5", None, ["192.168.1.5"], True),
    ("192.168.109.80", None, ["192.168.1.5"], False),          # phone hotspot
    ("192.168.1.5", "aa:bb:cc:dd:ee:ff", ["AA-BB-CC-DD-EE-FF"], True),
    ("192.168.1.5", "11:22:33:44:55:66", ["aa:bb:cc:dd:ee:ff"], False),  # same IP, other router
    ("10.0.0.1", "aa:bb:cc:dd:ee:ff", ["192.168.1.5", "aa:bb:cc:dd:ee:ff"], True),
])
def test_matches_home(gateway, mac, home, expected):
    assert link.matches_home(gateway, mac, home) is expected


def test_normalise_mac():
    assert link.normalise_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert link.normalise_mac("192.168.1.5") is None


def test_no_gateway_is_not_away(monkeypatch):
    monkeypatch.setattr(link, "default_gateway", lambda: None)
    assert link.away_from_home(["192.168.1.5"]) is None


def test_empty_list_never_away(monkeypatch):
    monkeypatch.setattr(link, "default_gateway", lambda: pytest.fail("not consulted"))
    assert link.away_from_home([]) is None


def test_away_names_the_gateway(monkeypatch):
    monkeypatch.setattr(link, "default_gateway", lambda: "192.168.109.80")
    assert "192.168.109.80" in link.away_from_home(["192.168.1.5"])


def test_mac_only_looked_up_when_configured(monkeypatch):
    monkeypatch.setattr(link, "default_gateway", lambda: "192.168.1.5")
    monkeypatch.setattr(link, "gateway_mac", lambda gw: pytest.fail("no MAC configured"))
    assert link.away_from_home(["192.168.1.5"]) is None


def away_cfg(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    cfg.data["network"]["home_gateways"] = ["192.168.1.5"]
    monkeypatch.setattr(link, "default_gateway", lambda: "192.168.109.80")
    return cfg


def test_sweep_and_speed_skip_when_away(tmp_path, monkeypatch):
    cfg = away_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(main.ping, "probe", lambda *a: pytest.fail("probed while away"))
    monkeypatch.setattr(main, "_speed_locked", lambda *a: pytest.fail("tested while away"))
    assert main.cmd_latency(cfg, argparse.Namespace(dry_run=False)) == 0
    assert main.run_speed(cfg, force=True) == (0, "skipped: not on the home network")
    assert main.run_speed(cfg, on_demand=True)[1] == "skipped: not on the home network"


def test_config_rejects_a_bare_string(tmp_path):
    cfg = make_config(tmp_path)
    cfg.data["network"]["home_gateways"] = "192.168.1.5"
    with pytest.raises(ConfigError):
        validate(cfg)
