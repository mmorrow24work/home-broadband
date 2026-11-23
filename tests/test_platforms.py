"""Cross-platform behaviour: Windows ping.exe, Windows adapters, Jetson routes.

All of these run on any OS — the parsers are fed captured output, so the
Windows paths are exercised in Linux CI and vice versa.
"""

from __future__ import annotations

import json
import sys

import pytest

from collector import link, paths
from collector.probes.ping import build_command, parse_ping_output, parse_windows_ping_output
from collector.publish import git_ssh_command

# ---------------------------------------------------------------- Windows ping
WIN_OK = """
Pinging 1.1.1.1 with 32 bytes of data:
Reply from 1.1.1.1: bytes=32 time=12ms TTL=57
Reply from 1.1.1.1: bytes=32 time=11ms TTL=57
Reply from 1.1.1.1: bytes=32 time=13ms TTL=57
Reply from 1.1.1.1: bytes=32 time=12ms TTL=57
Reply from 1.1.1.1: bytes=32 time=12ms TTL=57

Ping statistics for 1.1.1.1:
    Packets: Sent = 5, Received = 5, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 11ms, Maximum = 13ms, Average = 12ms
"""

WIN_PARTIAL = """
Pinging 8.8.8.8 with 32 bytes of data:
Reply from 8.8.8.8: bytes=32 time=10ms TTL=117
Request timed out.
Reply from 8.8.8.8: bytes=32 time=14ms TTL=117
Reply from 8.8.8.8: bytes=32 time=12ms TTL=117
Reply from 8.8.8.8: bytes=32 time=12ms TTL=117

Ping statistics for 8.8.8.8:
    Packets: Sent = 5, Received = 4, Lost = 1 (20% loss),
Approximate round trip times in milli-seconds:
    Minimum = 10ms, Maximum = 14ms, Average = 12ms
"""

# Windows counts the router's "unreachable" answers as received: 0% loss.
WIN_UNREACHABLE = """
Pinging 10.9.9.9 with 32 bytes of data:
Reply from 192.168.1.1: Destination host unreachable.
Reply from 192.168.1.1: Destination host unreachable.
Reply from 192.168.1.1: Destination host unreachable.
Reply from 192.168.1.1: Destination host unreachable.
Reply from 192.168.1.1: Destination host unreachable.

Ping statistics for 10.9.9.9:
    Packets: Sent = 5, Received = 5, Lost = 0 (0% loss),
"""

WIN_SUB_MS = """
Pinging 192.168.1.1 with 32 bytes of data:
Reply from 192.168.1.1: bytes=32 time<1ms TTL=64
Reply from 192.168.1.1: bytes=32 time<1ms TTL=64
Reply from 192.168.1.1: bytes=32 time=1ms TTL=64

Ping statistics for 192.168.1.1:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 1ms, Average = 0ms
"""

WIN_IPV6 = """
Pinging 2606:4700:4700::1111 with 32 bytes of data:
Reply from 2606:4700:4700::1111: time=9ms
Reply from 2606:4700:4700::1111: time=10ms

Ping statistics for 2606:4700:4700::1111:
    Packets: Sent = 2, Received = 2, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 9ms, Maximum = 10ms, Average = 9ms
"""

WIN_GERMAN = """
Ping wird ausgefuhrt fur 1.1.1.1 mit 32 Bytes Daten:
Antwort von 1.1.1.1: Bytes=32 Zeit=12ms TTL=57
Antwort von 1.1.1.1: Bytes=32 Zeit=14ms TTL=57

Ping-Statistik fur 1.1.1.1:
    Pakete: Gesendet = 2, Empfangen = 2, Verloren = 0
    (0% Verlust),
Ca. Zeitangaben in Millisek.:
    Minimum = 12ms, Maximum = 14ms, Mittelwert = 13ms
"""

WIN_TOTAL_LOSS = """
Pinging 192.168.1.99 with 32 bytes of data:
Request timed out.
Request timed out.

Ping statistics for 192.168.1.99:
    Packets: Sent = 2, Received = 0, Lost = 2 (100% loss),
"""

WIN_NO_HOST = (
    "Ping request could not find host nosuch.invalid. Please check the name and try again."
)


def test_windows_clean_run():
    r = parse_ping_output(WIN_OK)
    assert (r["sent"], r["recv"], r["loss_pct"]) == (5, 5, 0.0)
    assert r["rtt_min"] == 11 and r["rtt_max"] == 13
    assert r["rtt_avg"] == 12.0
    assert r["rtt_mdev"] == pytest.approx(0.632, abs=0.001)


def test_windows_partial_loss():
    r = parse_ping_output(WIN_PARTIAL)
    assert (r["sent"], r["recv"], r["loss_pct"]) == (5, 4, 20.0)
    assert r["rtt_avg"] == 12.0


def test_windows_unreachable_replies_are_loss_not_success():
    r = parse_ping_output(WIN_UNREACHABLE)
    assert r["sent"] == 5
    assert r["recv"] == 0
    assert r["loss_pct"] == 100.0
    assert r["rtt_avg"] is None


def test_windows_sub_millisecond_replies():
    r = parse_ping_output(WIN_SUB_MS)
    assert r["recv"] == 3
    assert r["rtt_min"] == 0.5
    assert r["rtt_max"] == 1.0


def test_windows_ipv6_replies_have_no_bytes_or_ttl():
    r = parse_ping_output(WIN_IPV6)
    assert (r["recv"], r["rtt_min"], r["rtt_max"]) == (2, 9, 10)


def test_windows_total_loss():
    r = parse_ping_output(WIN_TOTAL_LOSS)
    assert (r["sent"], r["recv"], r["loss_pct"]) == (2, 0, 100.0)


def test_windows_unknown_host_never_sent():
    assert parse_ping_output(WIN_NO_HOST)["sent"] == 0


def test_windows_summary_line_is_not_mistaken_for_a_reply():
    """'Minimum = 11ms' must not add phantom samples."""
    assert parse_windows_ping_output(WIN_OK)["recv"] == 5


def test_localised_windows_output_parses():
    """Only numbers and punctuation are matched, so German (with its wrapped
    loss line) parses the same as English."""
    r = parse_ping_output(WIN_GERMAN)
    assert (r["sent"], r["recv"], r["rtt_avg"]) == (2, 2, 13.0)


def test_windows_command_uses_windows_flags():
    cmd = build_command("1.1.1.1", count=5, timeout=6, family="ipv4", windows=True, binary="ping")
    assert cmd[:5] == ["ping", "-n", "5", "-w", "1200"]
    assert "-c" not in cmd and "-q" not in cmd and "-i" not in cmd
    assert "-4" in cmd and cmd[-1] == "1.1.1.1"


def test_linux_command_unchanged():
    cmd = build_command("1.1.1.1", count=5, timeout=6, windows=False, binary="ping")
    assert cmd[:4] == ["ping", "-n", "-q", "-c"]


# ------------------------------------------------------------- Jetson routes
JETSON_ROUTES = (
    "default via 192.168.1.5 dev enP1p5s0 proto dhcp metric 100 \n"
    "default via 192.168.55.100 dev l4tbr0 metric 32766 linkdown \n"
)


def test_jetson_usb_bridge_route_is_ignored():
    assert link.parse_default_routes(JETSON_ROUTES) == "enP1p5s0"


def test_lowest_metric_wins_regardless_of_order():
    text = (
        "default via 10.0.0.1 dev wlan0 proto dhcp metric 600\n"
        "default via 192.168.1.1 dev eth0 proto dhcp metric 100\n"
    )
    assert link.parse_default_routes(text) == "eth0"


def test_route_without_metric_counts_as_zero():
    assert link.parse_default_routes("default via 192.168.1.1 dev eth0\n") == "eth0"


def test_no_default_route():
    assert link.parse_default_routes("") is None


# ----------------------------------------------------------- Windows adapters
def _adapter(**kw):
    base = {
        "Name": "Ethernet",
        "ReceiveLinkSpeed": 1_000_000_000,
        "TransmitLinkSpeed": 1_000_000_000,
        "PhysicalMediaType": "802.3",
        "InterfaceDescription": "Intel(R) Ethernet Controller I225-V",
    }
    base.update(kw)
    return json.dumps(base)


def test_windows_gigabit_ethernet():
    assert link.parse_windows_adapter(_adapter()) == ("Ethernet", 1000)


def test_windows_fast_ethernet_is_reported():
    speed = 100_000_000
    payload = _adapter(ReceiveLinkSpeed=speed, TransmitLinkSpeed=speed)
    assert link.parse_windows_adapter(payload) == ("Ethernet", 100)


def test_windows_wifi_is_unknown_not_a_ceiling():
    payload = _adapter(Name="Wi-Fi", PhysicalMediaType="Native 802.11",
                       InterfaceDescription="Intel(R) Wi-Fi 6E AX211 160MHz")
    assert link.parse_windows_adapter(payload) == ("Wi-Fi", None)


def test_windows_connection_type():
    assert link.windows_connection(_adapter()) == "wired"
    wifi = _adapter(Name="Wi-Fi", PhysicalMediaType="Native 802.11",
                    InterfaceDescription="Intel(R) Wi-Fi 6E AX211 160MHz")
    assert link.windows_connection(wifi) == "wifi"
    assert link.windows_connection("") is None


def test_windows_garbage_is_unknown():
    assert link.parse_windows_adapter("") == (None, None)
    assert link.parse_windows_adapter("[1, 2]") == (None, None)


# --------------------------------------------------------------- publish ssh
def test_git_ssh_command_uses_forward_slashes():
    cmd = git_ssh_command(r"C:\ProgramData\broadband-monitor\ssh\id_ed25519")
    assert "\\" not in cmd
    assert '-i "C:/ProgramData/broadband-monitor/ssh/id_ed25519"' in cmd
    assert 'UserKnownHostsFile="C:/ProgramData/broadband-monitor/ssh/known_hosts"' in cmd
    assert "IdentitiesOnly=yes" in cmd and "StrictHostKeyChecking=accept-new" in cmd


# ----------------------------------------------------------------- paths
def test_find_binary_falls_back_to_the_interpreter_directory(monkeypatch, tmp_path):
    """pip-installed tools (speedtest-cli on Windows) live beside the venv's python."""
    exe = tmp_path / ("tool.exe" if sys.platform == "win32" else "tool")
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    monkeypatch.setenv("PATH", "")
    assert paths.find_binary("tool") is not None


def test_platform_defaults_are_absolute():
    for path in (paths.CONF_DIR, paths.DATA_DIR, paths.APP_DIR, paths.CONFIG_FILE):
        assert path.is_absolute()


# ------------------------------------------------- install-windows.ps1 contract
def test_windows_installer_config_rewrites_match_and_validate(tmp_path):
    """install-windows.ps1 edits config.yaml with these anchored (?m) regexes.

    Mirrors test_install_script.py for the Linux installer: rename a key in the
    example config and the Windows install silently stops matching.
    """
    import re
    from pathlib import Path

    import yaml

    from collector.config import load_config

    root = Path(__file__).resolve().parent.parent
    ps1 = (root / "scripts" / "install-windows.ps1").read_text()
    text = (root / "config" / "config.example.yaml").read_text()
    base = "C:/ProgramData/broadband-monitor"
    edits = [
        (r"^  engines: \[.*\]$", "  engines: [speedtest-cli, ookla, cloudflare]"),
        (r'^  path: ".*broadband\.db"', f'  path: "{base}/data/broadband.db"'),
        (r"^  work_dir: .*$", f'  work_dir: "{base}/data/pages"'),
        (r"^  repo_dir: .*$", '  repo_dir: "C:/Users/me/git/home-broadband"'),
        (r"^  ssh_key: .*$", f'  ssh_key: "{base}/ssh/id_ed25519"'),
        (r'^    binary: "speedtest"(\s+#.*)?$', f'    binary: "{base}/bin/speedtest.exe"'),
    ]
    for pattern, replacement in edits:
        assert pattern.replace("\\", "") in ps1.replace("\\", "") or pattern in ps1, (
            f"install-windows.ps1 no longer uses {pattern!r}; update this test"
        )
        text, n = re.subn(pattern, replacement, text, flags=re.M)
        assert n == 1, f"{pattern!r} matched {n} lines in config.example.yaml"

    parsed = yaml.safe_load(text)
    assert parsed["database"]["path"] == f"{base}/data/broadband.db"
    assert parsed["publish"]["ssh_key"] == f"{base}/ssh/id_ed25519"
    assert parsed["speed"]["ookla"]["binary"].endswith("speedtest.exe")

    target = tmp_path / "config.yaml"
    target.write_text(text)
    load_config(target)  # full validation
