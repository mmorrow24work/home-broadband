"""ICMP echo probe built on the system `ping` binary.

Using the system binary rather than raw sockets keeps the collector
unprivileged: on Linux (Raspberry Pi OS, Ubuntu on Jetson) iputils ping uses
ICMP datagram sockets, and on Windows ping.exe needs no special rights at all.

The two speak different dialects. iputils takes `-c count -i interval -w
deadline` and prints a min/avg/max/mdev summary; Windows takes `-n count -w
per-reply-timeout-ms`, always waits a second between echoes, prints integer
milliseconds and no deviation figure, and localises every word of its output.
"""

from __future__ import annotations

import ipaddress
import math
import re
import shutil
import subprocess
import time
from typing import Any

from ..paths import IS_WINDOWS

# "5 packets transmitted, 5 received, 0% packet loss, time 1004ms"
# "5 packets transmitted, 4 received, +1 errors, 20% packet loss, time 4056ms"
_COUNTS_RE = re.compile(
    r"(?P<sent>\d+)\s+packets transmitted,\s+(?P<recv>\d+)\s+(?:packets\s+)?received"
    r"(?:,\s*\+?\d+\s+(?:errors|duplicates))*"
    r",\s*(?P<loss>[\d.]+)%\s*packet loss"
)
# "rtt min/avg/max/mdev = 12.345/13.456/14.567/0.789 ms"
# "round-trip min/avg/max/stddev = 12.345/13.456/14.567/0.789 ms"
_RTT_RE = re.compile(
    r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*"
    r"(?P<min>[\d.]+)/(?P<avg>[\d.]+)/(?P<max>[\d.]+)/(?P<mdev>[\d.]+)"
)


# Windows. Matched on punctuation and numbers only, because the words are
# localised ("Reply from"/"Antwort von", "time"/"Zeit", "loss"/"Verlust"):
#   "Reply from 1.1.1.1: bytes=32 time=12ms TTL=57"     -> 12
#   "Reply from 192.168.1.1: bytes=32 time<1ms TTL=64"  -> sub-millisecond
#   "Reply from 2606:4700:4700::1111: time=9ms"         -> 9 (IPv6 has no bytes/TTL)
# The summary's "Minimum = 11ms" has a space after '=' and so never matches.
_WIN_REPLY_RE = re.compile(r"(?P<op>[=<])(?P<ms>\d+(?:[.,]\d+)?)\s?ms\b", re.IGNORECASE)
#   "Packets: Sent = 5, Received = 5, Lost = 0 (0% loss),"
_WIN_COUNTS_RE = re.compile(
    r"=\s*(?P<sent>\d+),\s*[^=,]+=\s*(?P<recv>\d+),\s*[^=,]+=\s*(?P<lost>\d+)\s*\(\s*\d+\s*%"
)

# Windows prints "time<1ms" for a sub-millisecond reply; record the midpoint.
_WIN_SUB_MS = 0.5


def parse_windows_ping_output(output: str) -> dict[str, Any]:
    """Parse Windows ping.exe output into the same shape as iputils.

    The "Received" counter cannot be trusted: Windows counts "Destination host
    unreachable" from the local router as a *received reply*, reporting 0% loss
    to a host that answered nothing. So replies are counted individually, and
    only those carrying a round-trip time count as received.
    """
    result: dict[str, Any] = {
        "sent": 0,
        "recv": 0,
        "loss_pct": 100.0,
        "rtt_min": None,
        "rtt_avg": None,
        "rtt_max": None,
        "rtt_mdev": None,
    }

    counts = _WIN_COUNTS_RE.search(output)
    if not counts:
        return result
    sent = int(counts.group("sent"))

    times: list[float] = []
    for line in output.splitlines():
        if _WIN_COUNTS_RE.search(line):
            continue
        match = _WIN_REPLY_RE.search(line)
        if match:
            ms = float(match.group("ms").replace(",", "."))
            times.append(_WIN_SUB_MS if match.group("op") == "<" else ms)

    recv = min(len(times), sent)
    result["sent"] = sent
    result["recv"] = recv
    result["loss_pct"] = round((sent - recv) / sent * 100, 1) if sent else 100.0
    if times:
        mean = sum(times) / len(times)
        result["rtt_min"] = min(times)
        result["rtt_avg"] = round(mean, 3)
        result["rtt_max"] = max(times)
        # iputils' mdev is the population standard deviation; match it.
        result["rtt_mdev"] = round(math.sqrt(sum((x - mean) ** 2 for x in times) / len(times)), 3)
    return result


def parse_ping_output(output: str) -> dict[str, Any]:
    """Parse iputils/BusyBox (or, failing those, Windows) ping text into a result dict.

    Always returns sent/recv/loss_pct; rtt_* are None when nothing came back.
    """
    result: dict[str, Any] = {
        "sent": 0,
        "recv": 0,
        "loss_pct": 100.0,
        "rtt_min": None,
        "rtt_avg": None,
        "rtt_max": None,
        "rtt_mdev": None,
    }

    counts = _COUNTS_RE.search(output)
    if counts:
        result["sent"] = int(counts.group("sent"))
        result["recv"] = int(counts.group("recv"))
        result["loss_pct"] = float(counts.group("loss"))

    rtt = _RTT_RE.search(output)
    if rtt:
        result["rtt_min"] = float(rtt.group("min"))
        result["rtt_avg"] = float(rtt.group("avg"))
        result["rtt_max"] = float(rtt.group("max"))
        result["rtt_mdev"] = float(rtt.group("mdev"))

    if not counts:
        return parse_windows_ping_output(output)
    return result


def _detect_family(host: str, configured: str) -> str:
    if configured in ("ipv4", "ipv6"):
        return configured
    try:
        return f"ipv{ipaddress.ip_address(host).version}"
    except ValueError:
        return "auto"


def build_command(
    host: str,
    *,
    count: int = 5,
    interval: float = 0.25,
    timeout: int = 6,
    family: str = "auto",
    binary: str | None = None,
    windows: bool = IS_WINDOWS,
) -> list[str]:
    ping = binary or shutil.which("ping") or "ping"
    if windows:
        # -w is a per-reply wait in ms, not a deadline; spread the budget so a
        # dead host still costs roughly `timeout` seconds, as it does on Linux.
        per_reply_ms = max(500, int(timeout * 1000 / max(count, 1)))
        cmd = [ping, "-n", str(count), "-w", str(per_reply_ms)]
    else:
        cmd = [ping, "-n", "-q", "-c", str(count), "-i", str(interval), "-w", str(int(timeout))]
    if family == "ipv4":
        cmd.append("-4")
    elif family == "ipv6":
        cmd.append("-6")
    cmd.append(host)
    return cmd


def probe(target: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Run one ICMP probe against a configured target."""
    host = target["host"]
    family = _detect_family(host, target.get("family", "auto"))
    count = int(settings.get("count", 5))
    timeout = int(settings.get("timeout_seconds", 6))

    row: dict[str, Any] = {
        "ts": int(time.time()),
        "target": target["name"],
        "host": host,
        "family": family if family != "auto" else "ipv4",
        "sent": count,
        "recv": 0,
        "loss_pct": 100.0,
        "rtt_min": None,
        "rtt_avg": None,
        "rtt_max": None,
        "rtt_mdev": None,
        "error": None,
    }

    cmd = build_command(
        host,
        count=count,
        interval=float(settings.get("ping_interval", 0.25)),
        timeout=timeout,
        family=family,
    )

    # Windows ping always paces echoes a second apart, so allow for that too.
    deadline = timeout + 5 + (count if IS_WINDOWS else 0)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            # Windows console tools write in the OEM code page, not the ANSI one.
            encoding="oem" if IS_WINDOWS else None,
            errors="replace",
            timeout=deadline,
            check=False,
        )
    except FileNotFoundError:
        row["error"] = "ping binary not found"
        return row
    except subprocess.TimeoutExpired:
        row["error"] = "ping timed out"
        return row

    parsed = parse_ping_output(proc.stdout + "\n" + proc.stderr)
    if parsed["sent"]:
        row.update(parsed)
    else:
        # ping never got as far as sending — DNS failure, no route, etc.
        stderr = (proc.stderr or proc.stdout).strip().splitlines()
        row["error"] = stderr[-1][:200] if stderr else f"ping exited {proc.returncode}"

    return row
