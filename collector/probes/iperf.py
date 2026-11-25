"""Local-network throughput: iperf3 against a machine on the LAN.

An internet speed test from a Wi-Fi laptop measures the line and the Wi-Fi
together. Testing against a wired box on the same LAN (`iperf3 -s` running on
the Jetson, say) measures the Wi-Fi alone, so the two side by side show which
of them is the bottleneck.

Each run is short — a few seconds each way — on purpose. A long, saturating
test can trip a consumer router's flood protection, which then drops this
machine's pings for minutes and reads as an outage on the dashboard.

The Wi-Fi link (band, channel, signal, negotiated rates) is recorded with each
result, because a slow run means little without knowing what the radio was
doing at the time.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any

from ..paths import IS_WINDOWS

log = logging.getLogger("broadband.iperf")

BUSY_RETRIES = 3   # the server runs one test at a time
BUSY_WAIT = 5.0


# ---------------------------------------------------------------------------
# iperf3
# ---------------------------------------------------------------------------
def find_binary(configured: str) -> str | None:
    """The iperf3 to run: the configured one, PATH, then Windows' usual places.

    A scheduled task runs as SYSTEM, whose PATH has none of a person's winget
    installs, so look where winget and the installer put it as well.
    """
    configured = (configured or "iperf3").strip()
    if os.path.isfile(configured):
        return configured
    found = shutil.which(configured)
    if found or not IS_WINDOWS:
        return found
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    candidates = [os.path.join(program_data, "broadband-monitor", "bin", "iperf3.exe")]
    candidates += sorted(glob.glob(
        r"C:\Users\*\AppData\Local\Microsoft\WinGet\Packages\*iPerf3*\iperf3.exe"
    ))
    return next((c for c in candidates if os.path.isfile(c)), None)


def parse_result(text: str) -> tuple[float | None, int | None, str | None]:
    """(received Mbit/s, retransmits, error) from `iperf3 -J` output."""
    try:
        data = json.loads(text)
    except ValueError:
        return None, None, (text.strip()[:200] or "no output from iperf3")
    if data.get("error"):
        return None, None, str(data["error"])[:200]
    end = data.get("end") or {}
    received = (end.get("sum_received") or {}).get("bits_per_second")
    retrans = (end.get("sum_sent") or {}).get("retransmits")
    if received is None:
        return None, None, "iperf3 reported no throughput"
    return round(received / 1e6, 2), retrans, None


def _one_way(binary: str, settings: dict[str, Any], reverse: bool) -> tuple:
    cmd = [
        binary, "-c", str(settings["server"]), "-p", str(int(settings.get("port", 5201))),
        "-t", str(int(settings.get("seconds", 5))), "-P", str(int(settings.get("streams", 4))),
        "-J",
    ]
    if reverse:
        cmd.append("-R")
    timeout = int(settings.get("seconds", 5)) + 30
    for attempt in range(BUSY_RETRIES):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return None, None, f"iperf3 timed out after {timeout}s"
        except OSError as exc:
            return None, None, f"cannot run iperf3: {exc}"
        result = parse_result(proc.stdout or proc.stderr)
        if result[2] and "busy" in result[2].lower() and attempt < BUSY_RETRIES - 1:
            time.sleep(BUSY_WAIT)
            continue
        return result
    return None, None, "iperf3 server stayed busy"


def run(settings: dict[str, Any]) -> dict[str, Any]:
    """Download (server -> here) then upload, plus the Wi-Fi link; one row."""
    row: dict[str, Any] = {
        "ts": int(time.time()), "server": str(settings.get("server") or ""), "ok": 0,
        "down_mbps": None, "up_mbps": None, "retransmits": None,
        "band": None, "channel": None, "rssi_dbm": None,
        "rx_rate_mbps": None, "tx_rate_mbps": None, "error": None,
    }
    row.update(wifi_link())
    binary = find_binary(settings.get("binary", "iperf3"))
    if not binary:
        row["error"] = "iperf3 not found (set lan.binary to its full path)"
        return row

    down, retrans_down, err_down = _one_way(binary, settings, reverse=True)
    up, retrans_up, err_up = _one_way(binary, settings, reverse=False)
    row["down_mbps"], row["up_mbps"] = down, up
    if retrans_down is not None or retrans_up is not None:
        row["retransmits"] = (retrans_down or 0) + (retrans_up or 0)
    errors = [e for e in (err_down, err_up) if e]
    row["error"] = "; ".join(errors) or None
    row["ok"] = int(down is not None and up is not None)
    return row


# ---------------------------------------------------------------------------
# Wi-Fi link
# ---------------------------------------------------------------------------
def _num(text: str | None) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(match.group()) if match else None


def parse_netsh(text: str) -> dict[str, Any]:
    """`netsh wlan show interfaces` (English) -> link fields. Empty if not on Wi-Fi."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields.setdefault(key.strip().lower(), value.strip())
    if not fields.get("state", "").lower().startswith("connected"):
        return {}
    channel = _num(fields.get("channel"))
    return {
        "band": fields.get("band") or None,
        "channel": int(channel) if channel is not None else None,
        "rssi_dbm": _num(fields.get("rssi")),
        "rx_rate_mbps": _num(fields.get("receive rate (mbps)")),
        "tx_rate_mbps": _num(fields.get("transmit rate (mbps)")),
    }


def _channel_from_freq(mhz: float) -> tuple[str, int] | None:
    if 2412 <= mhz <= 2484:
        return "2.4 GHz", 14 if mhz == 2484 else int((mhz - 2407) / 5)
    if 5150 <= mhz <= 5895:
        return "5 GHz", int((mhz - 5000) / 5)
    if 5925 <= mhz <= 7125:
        return "6 GHz", int((mhz - 5950) / 5)
    return None


def parse_iw(text: str) -> dict[str, Any]:
    """`iw dev <if> link` -> link fields. Empty when not associated."""
    if "Connected to" not in text:
        return {}
    out: dict[str, Any] = {}
    freq = re.search(r"freq:\s*([\d.]+)", text)
    if freq and (band := _channel_from_freq(float(freq.group(1)))):
        out["band"], out["channel"] = band
    sig = re.search(r"signal:\s*(-?\d+)", text)
    rx = re.search(r"rx bitrate:\s*([\d.]+)", text)
    tx = re.search(r"tx bitrate:\s*([\d.]+)", text)
    out["rssi_dbm"] = float(sig.group(1)) if sig else None
    out["rx_rate_mbps"] = float(rx.group(1)) if rx else None
    out["tx_rate_mbps"] = float(tx.group(1)) if tx else None
    return out


def wifi_link() -> dict[str, Any]:
    """Best effort: the radio's current state, or {} when wired or unknown."""
    try:
        if IS_WINDOWS:
            proc = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                                  capture_output=True, text=True, timeout=15, check=False)
            return parse_netsh(proc.stdout)
        from .. import link

        interface = link.default_interface()
        if not interface or link.connection_type(interface) != "wifi":
            return {}
        proc = subprocess.run(["iw", "dev", interface, "link"],
                              capture_output=True, text=True, timeout=15, check=False)
        return parse_iw(proc.stdout)
    except (OSError, subprocess.SubprocessError):
        return {}
