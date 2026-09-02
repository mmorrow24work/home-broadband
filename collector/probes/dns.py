"""DNS resolution timing.

Prefers dnspython (queries a named resolver directly, so results are not
polluted by the local stub cache). Falls back to `dig`, then to
socket.getaddrinfo if neither is available.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import time
from typing import Any

try:  # optional dependency
    import dns.exception
    import dns.message
    import dns.query
    import dns.rdatatype

    HAVE_DNSPYTHON = True
except ImportError:  # pragma: no cover - exercised only on hosts without dnspython
    HAVE_DNSPYTHON = False

_DIG_TIME_RE = re.compile(r";;\s*Query time:\s*(\d+)\s*msec")


def _via_dnspython(host: str, resolver: str, record: str, timeout: float) -> tuple[float, str]:
    query = dns.message.make_query(host, dns.rdatatype.from_text(record))
    started = time.perf_counter()
    response = dns.query.udp(query, resolver, timeout=timeout)
    elapsed_ms = (time.perf_counter() - started) * 1000
    answers = [
        item.to_text()
        for rrset in response.answer
        for item in rrset
        if rrset.rdtype == dns.rdatatype.from_text(record)
    ]
    return elapsed_ms, ",".join(answers[:4])


def _via_dig(host: str, resolver: str, record: str, timeout: float) -> tuple[float, str]:
    cmd = [
        "dig",
        f"@{resolver}",
        "+tries=1",
        f"+time={max(1, int(timeout))}",
        "+noall",
        "+answer",
        "+stats",
        record,
        host,
    ]
    started = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 3, check=False)
    wall_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "dig failed").strip()[:200])
    match = _DIG_TIME_RE.search(proc.stdout)
    elapsed_ms = float(match.group(1)) if match else wall_ms
    answers = [
        line.split()[-1]
        for line in proc.stdout.splitlines()
        if line and not line.startswith(";") and len(line.split()) >= 5
    ]
    if not answers:
        raise RuntimeError("NXDOMAIN or empty answer")
    return elapsed_ms, ",".join(answers[:4])


def _via_getaddrinfo(host: str, record: str, timeout: float) -> tuple[float, str]:
    family = socket.AF_INET6 if record == "AAAA" else socket.AF_INET
    socket.setdefaulttimeout(timeout)
    started = time.perf_counter()
    infos = socket.getaddrinfo(host, None, family)
    elapsed_ms = (time.perf_counter() - started) * 1000
    addresses = sorted({info[4][0] for info in infos})
    return elapsed_ms, ",".join(addresses[:4])


def probe(target: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the target's host against each configured resolver."""
    host = target["host"]
    record = target.get("dns_record") or settings.get("record", "A")
    timeout = float(settings.get("timeout_seconds", 3))
    resolvers = settings.get("resolvers") or ["system"]
    now = int(time.time())
    rows: list[dict[str, Any]] = []

    for resolver in resolvers:
        row: dict[str, Any] = {
            "ts": now,
            "target": target["name"],
            "host": host,
            "resolver": resolver,
            "ok": 0,
            "ms": None,
            "answer": None,
            "error": None,
        }
        try:
            if resolver == "system" or (not HAVE_DNSPYTHON and not shutil.which("dig")):
                elapsed, answer = _via_getaddrinfo(host, record, timeout)
            elif HAVE_DNSPYTHON:
                elapsed, answer = _via_dnspython(host, resolver, record, timeout)
            else:
                elapsed, answer = _via_dig(host, resolver, record, timeout)
            row.update(ok=1, ms=round(elapsed, 2), answer=answer)
        except Exception as exc:  # noqa: BLE001 - any resolver failure is a data point
            row["error"] = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            socket.setdefaulttimeout(None)
        rows.append(row)

    return rows
