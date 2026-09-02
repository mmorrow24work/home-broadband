"""Cloudflare speed test engine.

Talks directly to speed.cloudflare.com rather than depending on a third-party
wrapper library, so the measurement method is explicit and stable:

  * latency  — N small requests, report the minimum and the mean absolute
               successive difference (jitter), matching how Ookla defines it.
  * download — `streams` concurrent GETs of __down; bytes are counted by a
               shared atomic counter and throughput is measured only over the
               window *after* a warm-up period, so TCP slow start and the
               congestion-window ramp do not drag the number down.
  * upload   — the same shape against __up with a generated payload.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from typing import Any

import requests

log = logging.getLogger("broadband.cloudflare")

BASE = "https://speed.cloudflare.com"
CHUNK = 64 * 1024
WARMUP_SECONDS = 2.0
_PAYLOAD_BLOCK = b"\x00" * CHUNK


class _Counter:
    """Thread-safe byte counter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.value = 0

    def add(self, amount: int) -> None:
        with self._lock:
            self.value += amount

    def read(self) -> int:
        with self._lock:
            return self.value


class _UploadBody:
    """Generates upload payload until the stop event fires or the cap is hit."""

    def __init__(self, limit: int, stop: threading.Event, counter: _Counter):
        self.limit = limit
        self.stop = stop
        self.counter = counter

    def __iter__(self):
        sent = 0
        while sent < self.limit and not self.stop.is_set():
            block = _PAYLOAD_BLOCK[: min(CHUNK, self.limit - sent)]
            sent += len(block)
            self.counter.add(len(block))
            yield block


def measure_latency(session: requests.Session, samples: int = 12, timeout: float = 5.0):
    """Return (min_ms, jitter_ms, loss_pct) from small timed requests."""
    timings: list[float] = []
    failures = 0
    for _ in range(samples):
        started = time.perf_counter()
        try:
            response = session.get(
                f"{BASE}/__down", params={"bytes": 0}, timeout=timeout
            )
            response.raise_for_status()
            response.content  # noqa: B018 - force full read
            timings.append((time.perf_counter() - started) * 1000)
        except requests.RequestException:
            failures += 1
        time.sleep(0.05)

    if not timings:
        return None, None, 100.0

    jitter = (
        statistics.fmean(abs(b - a) for a, b in zip(timings, timings[1:]))
        if len(timings) > 1
        else 0.0
    )
    return round(min(timings), 2), round(jitter, 2), round(100 * failures / samples, 2)


def _download_worker(counter: _Counter, stop: threading.Event, chunk_bytes: int) -> None:
    session = requests.Session()
    try:
        while not stop.is_set():
            try:
                with session.get(
                    f"{BASE}/__down",
                    params={"bytes": chunk_bytes},
                    stream=True,
                    timeout=30,
                ) as response:
                    for block in response.iter_content(CHUNK):
                        counter.add(len(block))
                        if stop.is_set():
                            break
            except requests.RequestException:
                if stop.is_set():
                    return
                time.sleep(0.25)
    finally:
        session.close()


def _upload_worker(counter: _Counter, stop: threading.Event, chunk_bytes: int) -> None:
    session = requests.Session()
    try:
        while not stop.is_set():
            try:
                session.post(
                    f"{BASE}/__up",
                    data=_UploadBody(chunk_bytes, stop, counter),
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=30,
                )
            except requests.RequestException:
                if stop.is_set():
                    return
                time.sleep(0.25)
    finally:
        session.close()


def _run_direction(worker, streams: int, chunk_bytes: int, duration: float) -> tuple[float, int]:
    """Run `streams` workers and return (mbps, total_bytes_transferred)."""
    counter = _Counter()
    stop = threading.Event()
    threads = [
        threading.Thread(target=worker, args=(counter, stop, chunk_bytes), daemon=True)
        for _ in range(streams)
    ]
    for thread in threads:
        thread.start()

    warmup = min(WARMUP_SECONDS, duration / 3)
    time.sleep(warmup)
    start_bytes, start_time = counter.read(), time.perf_counter()

    time.sleep(max(0.5, duration - warmup))
    end_bytes, end_time = counter.read(), time.perf_counter()

    stop.set()
    for thread in threads:
        thread.join(timeout=10)

    elapsed = max(end_time - start_time, 0.001)
    mbps = (end_bytes - start_bytes) * 8 / elapsed / 1_000_000
    return round(mbps, 3), counter.read()


def parse_trace(text: str) -> dict[str, str]:
    """Parse Cloudflare's /cdn-cgi/trace key=value body."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def fetch_meta(session: requests.Session, timeout: float = 5.0) -> dict[str, Any]:
    """Which Cloudflare edge served us, and what it thinks our IP is.

    /meta used to return city/colo/ASN as JSON; Cloudflare now serves an empty
    object there, which is how "via None" ended up in the results table. The
    /cdn-cgi/trace endpoint is served by every Cloudflare-fronted host and still
    reports the colo, the client IP and the country, so fall back to it.
    """
    try:
        response = session.get(f"{BASE}/meta", timeout=timeout)
        response.raise_for_status()
        meta = response.json()
        if meta:
            return meta
    except (requests.RequestException, ValueError) as exc:
        log.debug("cloudflare /meta unavailable (%s)", exc)

    try:
        response = session.get(f"{BASE}/cdn-cgi/trace", timeout=timeout)
        response.raise_for_status()
        trace = parse_trace(response.text)
    except requests.RequestException as exc:
        log.warning("cloudflare edge details unavailable (%s)", exc)
        return {}

    if not trace.get("colo"):
        return {}
    # Present it in the same shape the old /meta gave us.
    return {
        "colo": trace.get("colo"),
        "city": trace.get("colo"),
        "clientIp": trace.get("ip"),
        "country": trace.get("loc"),
        "httpProtocol": trace.get("http"),
        "source": "cdn-cgi/trace",
    }


def run(settings: dict[str, Any]) -> dict[str, Any]:
    started = int(time.time())
    wall_start = time.perf_counter()
    row: dict[str, Any] = {
        "ts": started,
        "engine": "cloudflare",
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

    streams = max(1, int(settings.get("streams", 4)))
    duration = float(settings.get("duration_seconds", 12))
    down_cap = int(settings.get("download_bytes", 250_000_000))
    up_cap = int(settings.get("upload_bytes", 100_000_000))

    session = requests.Session()
    try:
        meta = fetch_meta(session)
        row["server"] = (
            ", ".join(part for part in (meta.get("city"), meta.get("colo")) if part)
            or meta.get("colo")
            or "Cloudflare edge"          # never leave the table showing "None"
        )
        row["server_id"] = meta.get("colo")
        row["isp"] = meta.get("asOrganization")
        row["external_ip"] = meta.get("clientIp")
        row["raw"] = {"meta": meta, "streams": streams, "duration_s": duration}

        ping_ms, jitter_ms, loss_pct = measure_latency(session)
        row.update(ping_ms=ping_ms, jitter_ms=jitter_ms, loss_pct=loss_pct)

        down_mbps, down_bytes = _run_direction(
            _download_worker, streams, max(CHUNK, down_cap // streams), duration
        )
        up_mbps, up_bytes = _run_direction(
            _upload_worker, streams, max(CHUNK, up_cap // streams), duration
        )

        row.update(
            ok=1 if down_mbps > 0 else 0,
            down_mbps=down_mbps,
            up_mbps=up_mbps,
            bytes_down=down_bytes,
            bytes_up=up_bytes,
        )
        if not row["ok"]:
            row["error"] = "no bytes transferred"
    except Exception as exc:  # noqa: BLE001 - a failed test is itself a data point
        row["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        session.close()
        row["duration_s"] = round(time.perf_counter() - wall_start, 2)

    return row


def estimated_bytes(settings: dict[str, Any]) -> int:
    """Rough per-run consumption, used by the daily data guard."""
    return int(settings.get("download_bytes", 0)) + int(settings.get("upload_bytes", 0))
