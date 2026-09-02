"""Ookla Speedtest CLI engine.

This wraps the *official* Ookla binary (`speedtest`), not the unrelated and
long-unmaintained `speedtest-cli` PyPI package. See docs/INSTALL.md for how to
add Ookla's apt repository on Raspberry Pi OS.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any


class OoklaNotInstalled(RuntimeError):
    pass


def _mbps(bandwidth_bytes_per_s: Any) -> float | None:
    """Ookla reports bandwidth in bytes/second."""
    if bandwidth_bytes_per_s in (None, ""):
        return None
    return round(float(bandwidth_bytes_per_s) * 8 / 1_000_000, 3)


def parse_result(payload: str) -> dict[str, Any]:
    """Convert one `speedtest -f json` result object into a speed table row."""
    data = json.loads(payload)
    if data.get("type") not in (None, "result"):
        raise ValueError(f"unexpected speedtest record type: {data.get('type')!r}")

    download = data.get("download") or {}
    upload = data.get("upload") or {}
    ping = data.get("ping") or {}
    server = data.get("server") or {}
    interface = data.get("interface") or {}
    result = data.get("result") or {}

    loss = data.get("packetLoss")
    try:
        loss = float(loss)
    except (TypeError, ValueError):
        loss = None

    elapsed = (float(download.get("elapsed") or 0) + float(upload.get("elapsed") or 0)) / 1000

    server_label = ", ".join(
        part for part in (server.get("name"), server.get("location")) if part
    )

    return {
        "engine": "ookla",
        "ok": 1,
        "down_mbps": _mbps(download.get("bandwidth")),
        "up_mbps": _mbps(upload.get("bandwidth")),
        "ping_ms": round(float(ping["latency"]), 2) if ping.get("latency") is not None else None,
        "jitter_ms": round(float(ping["jitter"]), 2) if ping.get("jitter") is not None else None,
        "loss_pct": loss,
        "server": server_label or server.get("host"),
        "server_id": str(server["id"]) if server.get("id") is not None else None,
        "isp": data.get("isp"),
        "external_ip": interface.get("externalIp"),
        "result_url": result.get("url"),
        "bytes_down": int(download.get("bytes") or 0),
        "bytes_up": int(upload.get("bytes") or 0),
        "duration_s": round(elapsed, 2) or None,
        "error": None,
        "raw": data,
    }


def build_command(settings: dict[str, Any]) -> list[str]:
    binary = settings.get("binary") or "speedtest"
    resolved = shutil.which(binary)
    if not resolved:
        raise OoklaNotInstalled(
            f"{binary!r} not found on PATH. Install the official Ookla CLI "
            "(see docs/INSTALL.md) — the python 'speedtest-cli' package will not work here."
        )
    cmd = [
        resolved,
        "--format=json",
        "--accept-license",
        "--accept-gdpr",
        "--progress=no",
    ]
    if settings.get("server_id"):
        cmd += ["--server-id", str(settings["server_id"])]
    cmd += [str(arg) for arg in settings.get("extra_args") or []]
    return cmd


def run(settings: dict[str, Any], timeout: int = 180) -> dict[str, Any]:
    started = int(time.time())
    row: dict[str, Any] = {
        "ts": started,
        "engine": "ookla",
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
    except OoklaNotInstalled as exc:
        row["error"] = str(exc)
        return row

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        row["error"] = f"speedtest exceeded {timeout}s"
        return row

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        row["error"] = (detail[-1] if detail else f"speedtest exited {proc.returncode}")[:300]
        return row

    try:
        parsed = parse_result(proc.stdout.strip().splitlines()[-1])
    except (ValueError, KeyError, IndexError) as exc:
        row["error"] = f"could not parse speedtest output: {exc}"[:300]
        return row

    row.update(parsed)
    row["ts"] = started
    return row
