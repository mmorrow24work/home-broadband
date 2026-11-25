"""Local-network (Wi-Fi) throughput via iperf3."""

import argparse
import json

import pytest

from collector import db, link, main
from collector import publish as pub
from collector.probes import iperf
from tests.test_publish import make_config

NETSH = """
There is 1 interface on the system:

    Name                   : WiFi
    Description            : Intel(R) Wi-Fi 6E AX211 160MHz
    State                  : connected
    SSID                   : meesh
    AP BSSID               : d6:92:5e:cf:c2:18
    Band                   : 5 GHz
    Channel                : 52
    Radio type             : 802.11ac
    Receive rate (Mbps)    : 292.5
    Transmit rate (Mbps)   : 866.7
    Signal                 : 96%
    Rssi                   : -52
"""

IW = """Connected to d6:92:5e:cf:c2:18 (on wlan0)
	SSID: meesh
	freq: 5260.0
	signal: -52 dBm
	rx bitrate: 292.5 MBit/s VHT-MCS 6 80MHz short GI VHT-NSS 1
	tx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2
"""


def iperf_json(mbps, retrans=0):
    return json.dumps({"end": {"sum_received": {"bits_per_second": mbps * 1e6},
                               "sum_sent": {"retransmits": retrans}}})


def test_parse_netsh():
    assert iperf.parse_netsh(NETSH) == {
        "band": "5 GHz", "channel": 52, "rssi_dbm": -52.0,
        "rx_rate_mbps": 292.5, "tx_rate_mbps": 866.7,
    }


def test_parse_netsh_disconnected():
    assert iperf.parse_netsh("    State                  : disconnected\n") == {}


def test_parse_iw():
    assert iperf.parse_iw(IW) == {
        "band": "5 GHz", "channel": 52, "rssi_dbm": -52.0,
        "rx_rate_mbps": 292.5, "tx_rate_mbps": 866.7,
    }
    assert iperf.parse_iw("Not connected.") == {}


def test_parse_result():
    assert iperf.parse_result(iperf_json(245.3, 7)) == (245.3, 7, None)
    assert iperf.parse_result('{"error": "unable to connect to server"}')[2] == \
        "unable to connect to server"
    assert iperf.parse_result("garbage")[0] is None


def test_run_measures_both_ways_and_retries_a_busy_server(monkeypatch):
    calls = []
    outputs = iter([
        '{"error": "the server is busy running a test. try again later"}',
        iperf_json(245.0, 3),   # download, after one busy retry
        iperf_json(475.0, 2),   # upload
    ])

    class Proc:
        def __init__(self, out):
            self.stdout, self.stderr = out, ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return Proc(next(outputs))

    monkeypatch.setattr(iperf, "find_binary", lambda b: "iperf3")
    monkeypatch.setattr(iperf, "wifi_link", lambda: {"channel": 52})
    monkeypatch.setattr(iperf.subprocess, "run", fake_run)
    monkeypatch.setattr(iperf, "BUSY_WAIT", 0)

    row = iperf.run({"server": "192.168.1.184", "seconds": 5, "streams": 4})
    assert row["ok"] == 1 and row["down_mbps"] == 245.0 and row["up_mbps"] == 475.0
    assert row["retransmits"] == 5 and row["channel"] == 52
    assert "-R" in calls[0] and "-R" in calls[1] and "-R" not in calls[2]


def test_run_without_binary_is_a_recorded_failure(monkeypatch):
    monkeypatch.setattr(iperf, "find_binary", lambda b: None)
    monkeypatch.setattr(iperf, "wifi_link", lambda: {})
    row = iperf.run({"server": "x"})
    assert row["ok"] == 0 and "not found" in row["error"]


def lan_cfg(tmp_path):
    cfg = make_config(tmp_path)
    cfg.data["lan"].update(enabled=True, server="192.168.1.184")
    return cfg


def test_cmd_lan_stores_and_publishes(tmp_path, monkeypatch):
    cfg = lan_cfg(tmp_path)
    db.init(cfg.db_path)
    monkeypatch.setattr(main.iperf, "run", lambda s: {
        "ts": 1_800_000_000, "server": s["server"], "ok": 1, "down_mbps": 245.0,
        "up_mbps": 475.0, "retransmits": 0, "band": "5 GHz", "channel": 52,
        "rssi_dbm": -52.0, "rx_rate_mbps": 292.5, "tx_rate_mbps": 866.7, "error": None,
    })
    assert main.cmd_lan(cfg, argparse.Namespace(dry_run=False)) == 0
    with db.connect(cfg.db_path, readonly=True) as conn:
        out = pub.export_lan(conn, 0, 2_000_000_000)
    assert out["down"] == [245.0] and out["channel"] == [52] and out["rx"] == [292.5]


def test_cmd_lan_is_a_noop_until_configured(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(main.iperf, "run", lambda s: pytest.fail("ran unconfigured"))
    assert main.cmd_lan(cfg, argparse.Namespace(dry_run=False)) == 0


def test_cmd_lan_skips_when_away(tmp_path, monkeypatch):
    cfg = lan_cfg(tmp_path)
    cfg.data["network"]["home_gateways"] = ["192.168.1.5"]
    monkeypatch.setattr(link, "default_gateway", lambda: "192.168.109.80")
    monkeypatch.setattr(main.iperf, "run", lambda s: pytest.fail("ran while away"))
    assert main.cmd_lan(cfg, argparse.Namespace(dry_run=False)) == 0
