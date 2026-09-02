"""HTTP(S) timing breakdown using curl's --write-out timers.

curl gives a clean split of DNS / TCP / TLS / TTFB without pulling extra
Python dependencies onto the Pi.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any

DEFAULT_EXPECT = ["200-399"]


def parse_expect(spec: Any) -> list[tuple[int, int]]:
    """Normalise an expect_status setting into inclusive (low, high) ranges.

    Accepts an int, a "NNN-NNN" range string, or a list mixing both. Plenty of
    healthy endpoints answer an unauthenticated probe with 401, 403 or 404 —
    treating those as failures produces a permanently red target that tells you
    nothing when the service really does break.
    """
    if spec is None:
        spec = DEFAULT_EXPECT
    if isinstance(spec, (int, str)):
        spec = [spec]

    ranges: list[tuple[int, int]] = []
    for item in spec:
        if isinstance(item, int):
            ranges.append((item, item))
            continue
        text = str(item).strip()
        if "-" in text:
            low, _, high = text.partition("-")
            ranges.append((int(low), int(high)))
        else:
            ranges.append((int(text), int(text)))
    return ranges


def status_matches(status: int, ranges: list[tuple[int, int]]) -> bool:
    return any(low <= status <= high for low, high in ranges)


_WRITE_OUT = (
    '{"http_code":%{http_code},'
    '"time_namelookup":%{time_namelookup},'
    '"time_connect":%{time_connect},'
    '"time_appconnect":%{time_appconnect},'
    '"time_starttransfer":%{time_starttransfer},'
    '"time_total":%{time_total},'
    '"size_download":%{size_download}}'
)


def parse_curl_timings(payload: str) -> dict[str, Any]:
    """Turn curl's cumulative second-based timers into per-phase milliseconds."""
    data = json.loads(payload)
    namelookup = float(data["time_namelookup"])
    connect = float(data["time_connect"])
    appconnect = float(data["time_appconnect"])
    starttransfer = float(data["time_starttransfer"])
    total = float(data["time_total"])

    # curl reports 0 for time_appconnect on plain HTTP.
    tls_ms = (appconnect - connect) * 1000 if appconnect > 0 else None

    return {
        "status": int(data["http_code"]),
        "dns_ms": round(namelookup * 1000, 2),
        "connect_ms": round((connect - namelookup) * 1000, 2),
        "tls_ms": round(tls_ms, 2) if tls_ms is not None else None,
        "ttfb_ms": round(starttransfer * 1000, 2),
        "total_ms": round(total * 1000, 2),
        "size_download": int(data["size_download"]),
    }


def probe(target: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    url = target["url"]
    timeout = int(settings.get("timeout_seconds", 10))
    row: dict[str, Any] = {
        "ts": int(time.time()),
        "target": target["name"],
        "url": url,
        "ok": 0,
        "status": None,
        "dns_ms": None,
        "connect_ms": None,
        "tls_ms": None,
        "ttfb_ms": None,
        "total_ms": None,
        "error": None,
    }

    curl = shutil.which("curl")
    if not curl:
        row["error"] = "curl not found"
        return row

    cmd = [
        curl,
        "--silent",
        "--show-error",
        "--location",
        "--output",
        "/dev/null",
        "--max-time",
        str(timeout),
        "--user-agent",
        str(settings.get("user_agent", "home-broadband-monitor/1.0")),
        "--write-out",
        _WRITE_OUT,
        url,
    ]
    if target.get("family") == "ipv4":
        cmd.insert(1, "-4")
    elif target.get("family") == "ipv6":
        cmd.insert(1, "-6")

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 5, check=False
        )
    except subprocess.TimeoutExpired:
        row["error"] = "curl timed out"
        return row

    if proc.returncode != 0 or not proc.stdout.strip():
        row["error"] = (proc.stderr or f"curl exited {proc.returncode}").strip()[:200]
        return row

    try:
        timings = parse_curl_timings(proc.stdout.strip())
    except (ValueError, KeyError) as exc:
        row["error"] = f"unparseable curl output: {exc}"[:200]
        return row

    expected = parse_expect(target.get("expect_status"))
    row.update(
        {
            "status": timings["status"],
            "dns_ms": timings["dns_ms"],
            "connect_ms": timings["connect_ms"],
            "tls_ms": timings["tls_ms"],
            "ttfb_ms": timings["ttfb_ms"],
            "total_ms": timings["total_ms"],
            "ok": 1 if status_matches(timings["status"], expected) else 0,
        }
    )
    if not row["ok"]:
        wanted = target.get("expect_status") or "2xx/3xx"
        row["error"] = f"HTTP {timings['status']} (expected {wanted})"
    return row
