"""Local network-interface facts.

A throughput result is meaningless without knowing what the Pi's own NIC can
carry. A Pi 3, Zero 2 W or a port negotiated at 100 Mbit tops out around
94 Mbps of usable TCP throughput — and a monitor that reports 92 Mbps on a
500 Mbps line, with no note attached, invites you to blame your ISP for your
own cabling.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("broadband.link")

SYS_NET = Path("/sys/class/net")

# Above this fraction of line rate, TCP throughput is bounded by the NIC rather
# than by the connection. Fast Ethernet in practice delivers ~94/100 = 0.94.
SATURATION_RATIO = 0.85


def default_interface() -> str | None:
    """The interface carrying the default route."""
    try:
        proc = subprocess.run(
            ["ip", "-o", "route", "show", "default"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    for line in proc.stdout.splitlines():
        parts = line.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    return None


def link_speed_mbps(interface: str | None = None) -> int | None:
    """Negotiated link speed in Mbit/s, or None if it cannot be determined.

    Wireless interfaces report a nominal rate that has little to do with real
    throughput, so they are reported as unknown rather than as a false ceiling.
    """
    interface = interface or default_interface()
    if not interface:
        return None

    if (SYS_NET / interface / "wireless").exists():
        return None

    try:
        raw = (SYS_NET / interface / "speed").read_text().strip()
        speed = int(raw)
    except (OSError, ValueError):
        return None

    # The kernel reports -1 for "unknown" and absurd values for virtual devices.
    return speed if 0 < speed <= 400_000 else None


def describe() -> dict[str, object]:
    interface = default_interface()
    return {
        "interface": interface,
        "link_speed_mbps": link_speed_mbps(interface),
    }


def warn_if_nic_bound(down_mbps: float | None, up_mbps: float | None) -> str | None:
    """Return a human-readable caveat when a result is capped by the NIC."""
    speed = link_speed_mbps()
    if not speed or not down_mbps:
        return None

    peak = max(down_mbps, up_mbps or 0)
    if peak < speed * SATURATION_RATIO:
        return None

    return (
        f"{peak:.0f} Mbps is at the ceiling of this machine's {speed} Mbit link — "
        "the measurement is bounded by the Pi's own network interface, not by "
        "your broadband. Check `ethtool` and use a Gigabit port on both ends."
    )
