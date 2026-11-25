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
import re
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


def parse_default_gateway(text: str) -> str | None:
    """The next hop of the route parse_default_routes picks, if it has one."""
    dev = parse_default_routes(text)
    for line in text.splitlines():
        parts = line.split()
        if "linkdown" in parts or "via" not in parts or "dev" not in parts:
            continue
        if parts[parts.index("dev") + 1] == dev:
            return parts[parts.index("via") + 1]
    return None


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


def _is_wireless_media(data: dict) -> bool:
    media = f"{data.get('PhysicalMediaType') or ''} {data.get('InterfaceDescription') or ''}"
    return "802.11" in media or "wireless" in media.lower() or "wi-fi" in media.lower()


def parse_windows_adapter(payload: str) -> tuple[str | None, int | None]:
    """(interface name, link speed in Mbit/s) from the PowerShell JSON above."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None

    name = data.get("Name") or None
    if _is_wireless_media(data):
        return name, None  # nominal Wi-Fi rates are not a throughput ceiling

    speeds = [data.get("ReceiveLinkSpeed"), data.get("TransmitLinkSpeed")]
    bps = [int(s) for s in speeds if isinstance(s, (int, float)) and s > 0]
    if not bps:
        return name, None
    mbps = int(min(bps) // 1_000_000)
    return name, mbps if 0 < mbps <= 400_000 else None


def windows_connection(payload: str) -> str | None:
    """'wifi' or 'wired' from the same PowerShell JSON, None if unreadable."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("Name"):
        return None
    return "wifi" if _is_wireless_media(data) else "wired"


@functools.lru_cache(maxsize=8)
def _windows_adapter_json(interface: str | None = None) -> str:
    safe = (interface or "").replace("'", "''")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_ADAPTER % safe],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        log.debug("adapter query failed: %s", proc.stderr.strip()[:200])
        return ""
    return proc.stdout.strip()


def _windows_adapter(interface: str | None = None) -> tuple[str | None, int | None]:
    return parse_windows_adapter(_windows_adapter_json(interface))


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


def connection_type(interface: str | None = None) -> str | None:
    """'wired' or 'wifi' for the interface carrying the default route.

    The dashboard shows it next to each collector: a Wi-Fi result includes the
    wireless hop, so it cannot be held against the line the way a wired one can.
    """
    if IS_WINDOWS:
        return windows_connection(_windows_adapter_json(interface))
    interface = interface or default_interface()
    if not interface:
        return None
    return "wifi" if (SYS_NET / interface / "wireless").exists() else "wired"


_PS_GATEWAY = (
    "(Find-NetRoute -RemoteIPAddress 1.1.1.1 | Where-Object NextHop | "
    "Select-Object -First 1).NextHop"
)


def default_gateway() -> str | None:
    """The router this machine actually uses, e.g. '192.168.1.5'."""
    if IS_WINDOWS:
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_GATEWAY]
    else:
        cmd = ["ip", "-o", "route", "show", "default"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if IS_WINDOWS:
        hop = proc.stdout.strip()
        return hop if hop and hop not in ("0.0.0.0", "::") else None
    return parse_default_gateway(proc.stdout)


_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$", re.IGNORECASE)


def normalise_mac(value: str | None) -> str | None:
    """'AA-BB-CC-DD-EE-FF' / 'aa:bb:…' -> 'aa:bb:cc:dd:ee:ff'; None if not a MAC."""
    value = (value or "").strip()
    return value.lower().replace("-", ":") if _MAC_RE.match(value) else None


def gateway_mac(gateway: str) -> str | None:
    """The gateway's hardware address from the neighbour (ARP/NDP) table."""
    if IS_WINDOWS:
        safe = gateway.replace("'", "''")
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
               f"(Get-NetNeighbor -IPAddress '{safe}' -ErrorAction SilentlyContinue | "
               "Select-Object -First 1).LinkLayerAddress"]
    else:
        cmd = ["ip", "neigh", "show", gateway]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    for word in proc.stdout.split():
        mac = normalise_mac(word)
        if mac and mac != "00:00:00:00:00:00":
            return mac
    return None


def matches_home(gateway: str | None, mac: str | None, home: list[str]) -> bool:
    """True when the gateway is one of `home` (IP addresses and/or MACs).

    A MAC entry is the stronger test: 192.168.1.1 is everyone's router, but
    only yours has its hardware address.
    """
    wanted = {str(h).strip().lower() for h in home if str(h).strip()}
    macs = {normalise_mac(h) for h in wanted} - {None}
    ips = {h for h in wanted if not normalise_mac(h)}
    return bool((gateway and gateway.lower() in ips) or (mac and mac in macs))


def away_from_home(home: list[str]) -> str | None:
    """Why this machine is not on the home network right now, or None.

    With no gateway at all (Wi-Fi off, waking from sleep) it is not "away":
    measuring then is what records a local network drop.
    """
    if not home:
        return None
    gateway = default_gateway()
    if not gateway:
        return None
    needs_mac = any(normalise_mac(str(h)) for h in home)
    mac = gateway_mac(gateway) if needs_mac else None
    if matches_home(gateway, mac, home):
        return None
    return f"gateway {gateway}{f' ({mac})' if mac else ''} is not a home gateway"


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
        "connection": connection_type(interface),
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
