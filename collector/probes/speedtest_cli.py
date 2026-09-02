"""`speedtest-cli` engine — the Python client packaged by Debian/Raspbian.

This is the third throughput engine, and it is *not* the same thing as the
official Ookla client in `ookla.py`, despite both talking to speedtest.net:

  * It is pure Python, so on a 32-bit Pi it is CPU-bound and will under-report
    a fast line. Treat it as a long-running consistency check rather than the
    authoritative number — see docs/OPERATIONS.md.
  * **It reports bits per second**, where the Ookla CLI reports bytes per
    second. Mixing the two up is an 8x error, so the conversion lives in one
    place here and is covered by a test.
  * It publishes no jitter or packet loss, so those columns stay NULL rather
    than being faked as zero.

Its virtue is that it installs from apt on any Debian derivative, including the
armhf builds where Ookla's repository has no packages at all.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from typing import Any


class SpeedtestCliNotInstalled(RuntimeError):
    pass


def _mbps(bits_per_second: Any) -> float | None:
    """speedtest-cli reports bits/second, unlike the Ookla CLI's bytes/second."""
    if bits_per_second in (None, ""):
        return None
    return round(float(bits_per_second) / 1_000_000, 3)


def parse_result(payload: str) -> dict[str, Any]:
    """Convert one `speedtest-cli --json` object into a speed table row."""
    data = json.loads(payload)
    if "download" not in data:
        raise ValueError("no 'download' key — not a speedtest-cli result object")

    server = data.get("server") or {}
    client = data.get("client") or {}

    server_label = ", ".join(
        part for part in (server.get("sponsor"), server.get("name")) if part
    )

    ping = data.get("ping")
    try:
        ping_ms = round(float(ping), 2)
    except (TypeError, ValueError):
        ping_ms = None

    return {
        "engine": "speedtest-cli",
        "ok": 1,
        "down_mbps": _mbps(data.get("download")),
        "up_mbps": _mbps(data.get("upload")),
        "ping_ms": ping_ms,
        # speedtest-cli measures neither of these; leave them unknown.
        "jitter_ms": None,
        "loss_pct": None,
        "server": server_label or server.get("host"),
        "server_id": str(server["id"]) if server.get("id") is not None else None,
        "isp": client.get("isp"),
        "external_ip": client.get("ip"),
        "result_url": data.get("share"),
        "bytes_down": int(data.get("bytes_received") or 0),
        "bytes_up": int(data.get("bytes_sent") or 0),
        "duration_s": None,
        "error": None,
        "raw": data,
    }


def build_command(settings: dict[str, Any]) -> list[str]:
    binary = settings.get("binary") or "speedtest-cli"
    resolved = shutil.which(binary)
    if not resolved:
        raise SpeedtestCliNotInstalled(
            f"{binary!r} not found on PATH. Install it with: sudo apt install speedtest-cli"
        )

    cmd = [resolved, "--json"]
    if settings.get("secure", True):
        cmd.append("--secure")
    # Pre-allocating the upload payload costs ~100 MB of RAM; on a Pi Zero or a
    # 512 MB Pi 3 that is the difference between a result and an OOM kill.
    if settings.get("no_pre_allocate", True):
        cmd.append("--no-pre-allocate")
    if settings.get("single"):
        cmd.append("--single")
    if settings.get("share"):
        # Uploads the result to speedtest.net and returns a shareable image URL.
        cmd.append("--share")
    if settings.get("server_id"):
        cmd += ["--server", str(settings["server_id"])]
    if settings.get("source_ip"):
        cmd += ["--source", str(settings["source_ip"])]
    cmd += ["--timeout", str(int(settings.get("timeout_seconds", 30)))]
    cmd += [str(arg) for arg in settings.get("extra_args") or []]
    return cmd


def run(settings: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    started = int(time.time())
    wall_start = time.perf_counter()
    row: dict[str, Any] = {
        "ts": started,
        "engine": "speedtest-cli",
        "ok": 0,
        "down_mbps": None,
        "up_mbps": None,
        "ping_ms": None,
        "jitter_ms": None,
        "loss_pct": None,
        "server": None,
        "server_id": None,
        "isp": None,
        "external_ip": None,
        "result_url": None,
        "bytes_down": 0,
        "bytes_up": 0,
        "duration_s": None,
        "error": None,
        "raw": None,
    }

    try:
        cmd = build_command(settings)
    except SpeedtestCliNotInstalled as exc:
        row["error"] = str(exc)
        return row

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        row["error"] = f"speedtest-cli exceeded {timeout}s"
        row["duration_s"] = round(time.perf_counter() - wall_start, 2)
        return row

    row["duration_s"] = round(time.perf_counter() - wall_start, 2)

    if proc.returncode != 0 or not proc.stdout.strip():
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        row["error"] = (detail[-1] if detail else f"speedtest-cli exited {proc.returncode}")[:300]
        return row

    try:
        # Warnings are written to stderr, but be defensive about stray stdout lines.
        parsed = parse_result(proc.stdout.strip().splitlines()[-1])
    except (ValueError, KeyError, IndexError) as exc:
        row["error"] = f"could not parse speedtest-cli output: {exc}"[:300]
        return row

    duration = row["duration_s"]
    row.update(parsed)
    row["ts"] = started
    row["duration_s"] = duration
    return row


# "12345) Sponsor Name (City, Country) [123.45 km]"
_LIST_RE = re.compile(
    r"^\s*(?P<id>\d+)\)\s+(?P<sponsor>.+?)\s+\((?P<location>[^)]*)\)\s+\[(?P<km>[\d.]+)\s*km\]"
)


def parse_server_list(output: str) -> list[dict[str, Any]]:
    """Parse `speedtest-cli --list` into structured rows.

    The list is ordered by speedtest.net's idea of where your IP is, which is
    frequently wrong on carrier ranges — hence the ability to filter by country
    rather than trusting the ordering.
    """
    servers = []
    for line in output.splitlines():
        match = _LIST_RE.match(line)
        if not match:
            continue
        location = match.group("location")
        city, _, country = location.rpartition(",")
        servers.append(
            {
                "id": match.group("id"),
                "sponsor": match.group("sponsor").strip(),
                "city": (city or location).strip(),
                "country": country.strip() or location.strip(),
                "km": float(match.group("km")),
            }
        )
    return servers


def list_servers(settings: dict[str, Any], timeout: int = 60) -> list[dict[str, Any]]:
    cmd = [shutil.which(settings.get("binary") or "speedtest-cli") or "speedtest-cli", "--list"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "speedtest-cli --list failed").strip()[:300])
    return parse_server_list(proc.stdout)
