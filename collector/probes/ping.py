"""ICMP echo probe built on the system `ping` binary (iputils).

Using the system binary rather than raw sockets keeps the collector
unprivileged on Raspberry Pi OS, where ping uses ICMP datagram sockets.
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
import time
from typing import Any

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


def parse_ping_output(output: str) -> dict[str, Any]:
    """Parse iputils/BusyBox ping summary text into a result dict.

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
) -> list[str]:
    ping = binary or shutil.which("ping") or "ping"
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

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
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
