import json

import pytest

from collector.probes.http import parse_curl_timings
from collector.probes.ookla import parse_result
from collector.probes.speedtest_cli import build_command
from collector.probes.speedtest_cli import parse_result as parse_stcli

OOKLA_JSON = json.dumps(
    {
        "type": "result",
        "timestamp": "2026-08-30T09:00:00Z",
        "ping": {"jitter": 0.842, "latency": 9.117, "low": 8.9, "high": 10.2},
        "download": {"bandwidth": 58_600_000, "bytes": 703_000_000, "elapsed": 12004},
        "upload": {"bandwidth": 8_900_000, "bytes": 107_000_000, "elapsed": 11003},
        "packetLoss": 0,
        "isp": "Community Fibre",
        "interface": {"externalIp": "203.0.113.45", "name": "eth0"},
        "server": {"id": 12345, "name": "Faelix", "location": "London", "host": "ldn.example"},
        "result": {"url": "https://www.speedtest.net/result/c/abc-123"},
    }
)


def test_bandwidth_bytes_convert_to_megabits():
    row = parse_result(OOKLA_JSON)
    # 58.6 MB/s * 8 = 468.8 Mbps — getting this wrong by 8x is the classic bug
    assert row["down_mbps"] == pytest.approx(468.8, abs=0.01)
    assert row["up_mbps"] == pytest.approx(71.2, abs=0.01)


def test_metadata_is_carried_through():
    row = parse_result(OOKLA_JSON)
    assert row["engine"] == "ookla"
    assert row["ok"] == 1
    assert row["ping_ms"] == 9.12
    assert row["jitter_ms"] == 0.84
    assert row["server"] == "Faelix, London"
    assert row["server_id"] == "12345"
    assert row["isp"] == "Community Fibre"
    assert row["result_url"].startswith("https://www.speedtest.net/result/")
    assert row["bytes_down"] + row["bytes_up"] == 810_000_000


def test_unavailable_packet_loss_is_null_not_zero():
    payload = json.loads(OOKLA_JSON)
    payload["packetLoss"] = "Not available"
    assert parse_result(json.dumps(payload))["loss_pct"] is None


def test_missing_optional_blocks_do_not_raise():
    payload = {"type": "result", "download": {"bandwidth": 1_000_000}}
    row = parse_result(json.dumps(payload))
    assert row["down_mbps"] == 8.0
    assert row["up_mbps"] is None
    assert row["server"] is None


CURL_HTTPS = (
    '{"http_code":200,"time_namelookup":0.012,"time_connect":0.031,'
    '"time_appconnect":0.088,"time_starttransfer":0.142,"time_total":0.190,'
    '"size_download":51234}'
)
CURL_HTTP = (
    '{"http_code":301,"time_namelookup":0.010,"time_connect":0.025,'
    '"time_appconnect":0.000,"time_starttransfer":0.061,"time_total":0.070,'
    '"size_download":0}'
)


def test_curl_phases_are_differences_not_cumulative_totals():
    timings = parse_curl_timings(CURL_HTTPS)
    assert timings["dns_ms"] == 12.0
    assert timings["connect_ms"] == 19.0  # 31 - 12, the TCP handshake alone
    assert timings["tls_ms"] == 57.0  # 88 - 31
    assert timings["ttfb_ms"] == 142.0
    assert timings["total_ms"] == 190.0


def test_plain_http_has_no_tls_phase():
    assert parse_curl_timings(CURL_HTTP)["tls_ms"] is None


# ---------------------------------------------------------------------------
# speedtest-cli (Debian's python client) — a different program from Ookla's,
# with a different unit for throughput. That difference is the whole test.
# ---------------------------------------------------------------------------
STCLI_JSON = json.dumps(
    {
        "download": 468800000.0,
        "upload": 71200000.0,
        "ping": 9.117,
        "server": {
            "url": "http://ldn.example:8080/speedtest/upload.php",
            "name": "London",
            "country": "United Kingdom",
            "cc": "GB",
            "sponsor": "Faelix",
            "id": "12345",
            "host": "ldn.example:8080",
            "latency": 9.117,
        },
        "timestamp": "2026-08-30T09:00:00.000000Z",
        "bytes_sent": 107000000,
        "bytes_received": 703000000,
        "share": None,
        "client": {"ip": "203.0.113.45", "isp": "Community Fibre", "country": "GB"},
    }
)


def test_speedtest_cli_reports_bits_not_bytes():
    """The Ookla CLI reports bytes/s and this one reports bits/s.

    Applying the wrong conversion is an 8x error in either direction, so both
    engines are pinned to the same real-world line speed here: 468.8 Mbps down.
    """
    row = parse_stcli(STCLI_JSON)
    assert row["down_mbps"] == pytest.approx(468.8, abs=0.01)
    assert row["up_mbps"] == pytest.approx(71.2, abs=0.01)

    # Same line, same numbers, different engine and different source units.
    ookla_row = parse_result(OOKLA_JSON)
    assert row["down_mbps"] == pytest.approx(ookla_row["down_mbps"], abs=0.01)
    assert row["up_mbps"] == pytest.approx(ookla_row["up_mbps"], abs=0.01)


def test_speedtest_cli_metadata():
    row = parse_stcli(STCLI_JSON)
    assert row["engine"] == "speedtest-cli"
    assert row["ok"] == 1
    assert row["ping_ms"] == 9.12
    assert row["server"] == "Faelix, London"
    assert row["server_id"] == "12345"
    assert row["isp"] == "Community Fibre"
    assert row["external_ip"] == "203.0.113.45"
    assert row["bytes_down"] == 703000000
    assert row["bytes_up"] == 107000000


def test_speedtest_cli_leaves_unmeasured_fields_null():
    """It publishes no jitter or loss — those must be NULL, not a fake zero."""
    row = parse_stcli(STCLI_JSON)
    assert row["jitter_ms"] is None
    assert row["loss_pct"] is None
    assert row["result_url"] is None  # no --share, so no URL


def test_speedtest_cli_share_url_is_captured():
    payload = json.loads(STCLI_JSON)
    payload["share"] = "http://www.speedtest.net/result/123456.png"
    assert parse_stcli(json.dumps(payload))["result_url"].endswith(".png")


def test_speedtest_cli_rejects_foreign_json():
    """An Ookla result must not be silently misread as a speedtest-cli one."""
    with pytest.raises(ValueError, match="not a speedtest-cli result"):
        parse_stcli(json.dumps({"type": "result", "ping": {"latency": 9}}))


def test_speedtest_cli_command_defaults(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/speedtest-cli")
    cmd = build_command({})
    assert cmd[:2] == ["/usr/bin/speedtest-cli", "--json"]
    assert "--secure" in cmd
    # Pre-allocation is what OOM-kills the client on a 512 MB Pi.
    assert "--no-pre-allocate" in cmd
    assert "--share" not in cmd
    assert "--server" not in cmd


def test_speedtest_cli_command_options(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/speedtest-cli")
    cmd = build_command(
        {"server_id": 12345, "share": True, "single": True, "no_pre_allocate": False,
         "secure": False, "timeout_seconds": 45, "extra_args": ["--bytes"]}
    )
    assert cmd[cmd.index("--server") + 1] == "12345"
    assert cmd[cmd.index("--timeout") + 1] == "45"
    assert "--share" in cmd and "--single" in cmd and "--bytes" in cmd
    assert "--no-pre-allocate" not in cmd and "--secure" not in cmd


def test_missing_binary_is_a_failed_row_not_an_exception(monkeypatch):
    from collector.probes import speedtest_cli

    monkeypatch.setattr("shutil.which", lambda _: None)
    row = speedtest_cli.run({})
    assert row["ok"] == 0
    assert "not found on PATH" in row["error"]
    assert row["engine"] == "speedtest-cli"
