"""Local network-interface facts.

A throughput result is meaningless without knowing what the collector's own NIC
can carry. A Pi 3, Zero 2 W or a port negotiated at 100 Mbit tops out around
94 Mbps of usable TCP throughput — and a monitor that reports 92 Mbps on a
500 Mbps line, with no note attached, invites you to blame your ISP for your
own cabling.

Linux reads the kernel's view from `ip route` and /sys/class/net. Windows asks
PowerShell's NetTCPIP / NetAdapter modules, which ship with every Windows 10/11.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import platform
import socket
import subprocess
from pathlib import Path

from .paths import IS_WINDOWS

log = logging.getLogger("broadband.link")

SYS_NET = Path("/sys/class/net")

# Above this fraction of line rate, TCP throughput is bounded by the NIC rather
# than by the connection. Fast Ethernet in practice delivers ~94/100 = 0.94.
SATURATION_RATIO = 0.85


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------
def parse_default_routes(text: str) -> str | None:
    """Pick the interface the kernel will actually use for the default route.

    There can be several. An NVIDIA Jetson, for one, always carries a fallback
    default via its USB device-mode bridge:

        default via 192.168.1.1 dev enP1p5s0 proto dhcp metric 100
        default via 192.168.55.100 dev l4tbr0 metric 32766 linkdown

    so take the lowest metric among routes whose link is up, not the first line.
    """
    best: tuple[int, int, str] | None = None
    for order, line in enumerate(text.splitlines()):
        parts = line.split()
        if "dev" not in parts or "linkdown" in parts:
            continue
        dev = parts[parts.index("dev") + 1]
        metric = 0
        if "metric" in parts:
            with contextlib.suppress(IndexError, ValueError):
                metric = int(parts[parts.index("metric") + 1])
        candidate = (metric, order, dev)
        if best is None or candidate < best:
            best = candidate
    return best[2] if best else None


def _linux_default_interface() -> str | None:
    try:
        proc = subprocess.run(
            ["ip", "-o", "route", "show", "default"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_default_routes(proc.stdout)


def _linux_link_speed(interface: str) -> int | None:
    if (SYS_NET / interface / "wireless").exists():
        return None
    try:
        raw = (SYS_NET / interface / "speed").read_text().strip()
        speed = int(raw)
    except (OSError, ValueError):
        return None
    # The kernel reports -1 for "unknown" and absurd values for virtual devices.
    return speed if 0 < speed <= 400_000 else None


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
# Find-NetRoute asks the routing table which interface would carry traffic to a
# public address — the Windows equivalent of `ip route get` — so metrics, VPNs
# and multiple gateways are resolved the same way the OS resolves them.
_PS_ADAPTER = r"""
$ErrorActionPreference = 'Stop'
$name = '%s'
if ($name) { $a = Get-NetAdapter -Name $name }
else {
  $idx = (Find-NetRoute -RemoteIPAddress 1.1.1.1 | Select-Object -First 1).InterfaceIndex
  $a = Get-NetAdapter -InterfaceIndex $idx
}
[pscustomobject]@{
  Name = $a.Name; ReceiveLinkSpeed = $a.ReceiveLinkSpeed; TransmitLinkSpeed = $a.TransmitLinkSpeed
  PhysicalMediaType = $a.PhysicalMediaType; InterfaceDescription = $a.InterfaceDescription
} | ConvertTo-Json -Compress
"""


def parse_windows_adapter(payload: str) -> tuple[str | None, int | None]:
    """(interface name, link speed in Mbit/s) from the PowerShell JSON above."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None

    name = data.get("Name") or None
    media = f"{data.get('PhysicalMediaType') or ''} {data.get('InterfaceDescription') or ''}"
    if "802.11" in media or "wireless" in media.lower() or "wi-fi" in media.lower():
        return name, None  # nominal Wi-Fi rates are not a throughput ceiling

    speeds = [data.get("ReceiveLinkSpeed"), data.get("TransmitLinkSpeed")]
    bps = [int(s) for s in speeds if isinstance(s, (int, float)) and s > 0]
    if not bps:
        return name, None
    mbps = int(min(bps) // 1_000_000)
    return name, mbps if 0 < mbps <= 400_000 else None


@functools.lru_cache(maxsize=8)
def _windows_adapter(interface: str | None = None) -> tuple[str | None, int | None]:
    safe = (interface or "").replace("'", "''")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_ADAPTER % safe],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if proc.returncode != 0:
        log.debug("adapter query failed: %s", proc.stderr.strip()[:200])
        return None, None
    return parse_windows_adapter(proc.stdout.strip())


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def default_interface() -> str | None:
    """The interface carrying the default route."""
    if IS_WINDOWS:
        return _windows_adapter(None)[0]
    return _linux_default_interface()


def link_speed_mbps(interface: str | None = None) -> int | None:
    """Negotiated link speed in Mbit/s, or None if it cannot be determined.

    Wireless interfaces report a nominal rate that has little to do with real
    throughput, so they are reported as unknown rather than as a false ceiling.
    """
    if IS_WINDOWS:
        return _windows_adapter(interface)[1]
    interface = interface or default_interface()
    if not interface:
        return None
    return _linux_link_speed(interface)


# DMI strings that boards ship unfilled; never worth showing.
_DMI_JUNK = {"", "default string", "to be filled by o.e.m.", "system product name", "none"}


def device_model() -> str:
    """A human name for the collector hardware, e.g. 'NVIDIA Jetson AGX Orin 64GB'.

    ARM boards (Pi, Jetson) describe themselves in the device tree; PCs in DMI.
    """
    try:
        model = Path("/proc/device-tree/model").read_bytes().rstrip(b"\0").decode().strip()
        if model:
            return model
    except OSError:
        pass
    parts = []
    for name in ("sys_vendor", "product_name"):
        try:
            value = (Path("/sys/class/dmi/id") / name).read_text().strip()
        except OSError:
            continue
        if value.lower() not in _DMI_JUNK and value not in parts:
            parts.append(value)
    if parts:
        return " ".join(parts)
    return "Windows PC" if IS_WINDOWS else f"{platform.system() or 'Unknown'} machine"


def describe() -> dict[str, object]:
    interface = default_interface()
    return {
        "interface": interface,
        "link_speed_mbps": link_speed_mbps(interface),
        "hostname": socket.gethostname().split(".")[0],
        "model": device_model(),
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
        "the measurement is bounded by the collector's own network interface, not by "
        "your broadband. Check the cable and use a Gigabit port on both ends."
    )
