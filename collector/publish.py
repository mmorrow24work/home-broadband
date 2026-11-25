"""Aggregate SQLite into compact JSON and publish it to a GitHub Pages branch.

Design notes
------------
* The Pi's SQLite file is the system of record. Everything published is a
  derived view, so the published branch carries **no history**: by default the
  publisher rewrites it as a single orphan commit and force-pushes. The repo
  therefore stays the size of one snapshot no matter how many years it runs.
* JSON is written column-wise ({"t": [...], "rtt": [...]}) rather than as an
  array of objects. For a year of 5-minute buckets that is roughly a 4x saving
  over the naive layout and parses faster in the browser.
* The last `latest_hours` are published at full probe resolution; everything
  older is bucketed to `bucket_seconds`, and whole months are rolled up hourly.
* Several collectors can share one site — a wired box and a Wi-Fi laptop, say.
  Each owns data/<collector id>/ and nothing else: before pushing, a publisher
  fetches the branch, carries every other collector's directory across into its
  new snapshot, rebuilds data/collectors.json (the dashboard's switcher) and
  pushes with --force-with-lease, retrying if another collector got in first.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import random
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__, db, link
from .config import Config
from .paths import find_git

log = logging.getLogger("broadband.publish")

DATA_DIRNAME = "data"
COLLECTORS_INDEX = "collectors.json"
PUSH_ATTEMPTS = 4
# Resolved once per publish from publish.git_binary; see paths.find_git.
GIT = "git"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile. Small sample sizes make interpolation noise."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    rank = max(1, min(len(clean), math.ceil(pct / 100 * len(clean))))
    return clean[rank - 1]


def _round(value: Any, digits: int = 2) -> Any:
    return round(value, digits) if isinstance(value, (int, float)) else value


def _write_json(path: Path, payload: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"), default=str)
    path.write_text(text, encoding="utf-8")
    return len(text)


def _day_bounds(day: dt.date, tz: dt.tzinfo) -> tuple[int, int]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    return int(start.timestamp()), int((start + dt.timedelta(days=1)).timestamp())


def _tzinfo(name: str) -> dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - fall back to UTC on odd systems
        log.warning("unknown timezone %r, falling back to UTC", name)
        return dt.timezone.utc


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def export_latency(conn, start: int, end: int, bucket: int | None) -> dict[str, Any]:
    """Column-wise latency series, optionally bucketed."""
    series: dict[str, dict[str, list]] = {}

    if bucket:
        rows = conn.execute(
            """SELECT target,
                      (ts / :b) * :b            AS bucket,
                      AVG(rtt_avg)              AS rtt,
                      MAX(rtt_max)              AS rtt_max,
                      AVG(loss_pct)             AS loss,
                      COUNT(*)                  AS n,
                      SUM(CASE WHEN loss_pct >= 100 THEN 1 ELSE 0 END) AS dead
               FROM latency
               WHERE ts >= :start AND ts < :end
               GROUP BY target, bucket
               ORDER BY bucket""",
            {"b": bucket, "start": start, "end": end},
        ).fetchall()
        for row in rows:
            entry = series.setdefault(
                row["target"], {"t": [], "rtt": [], "rtt_max": [], "loss": [], "n": []}
            )
            entry["t"].append(row["bucket"])
            entry["rtt"].append(_round(row["rtt"], 2))
            entry["rtt_max"].append(_round(row["rtt_max"], 2))
            entry["loss"].append(_round(row["loss"], 2))
            entry["n"].append(row["n"])
    else:
        rows = conn.execute(
            """SELECT ts, target, rtt_avg, rtt_max, loss_pct
               FROM latency WHERE ts >= ? AND ts < ? ORDER BY ts""",
            (start, end),
        ).fetchall()
        for row in rows:
            entry = series.setdefault(
                row["target"], {"t": [], "rtt": [], "rtt_max": [], "loss": []}
            )
            entry["t"].append(row["ts"])
            entry["rtt"].append(_round(row["rtt_avg"], 2))
            entry["rtt_max"].append(_round(row["rtt_max"], 2))
            entry["loss"].append(_round(row["loss_pct"], 2))

    return series


def export_speed(conn, start: int, end: int) -> dict[str, list]:
    rows = conn.execute(
        """SELECT ts, engine, ok, down_mbps, up_mbps, ping_ms, jitter_ms, loss_pct,
                  server, isp, result_url, bytes_down, bytes_up, error
           FROM speed WHERE ts >= ? AND ts < ? ORDER BY ts""",
        (start, end),
    ).fetchall()
    out: dict[str, list] = {
        "t": [], "engine": [], "ok": [], "down": [], "up": [], "ping": [],
        "jitter": [], "loss": [], "server": [], "isp": [], "url": [], "gb": [],
        "error": [],
    }
    for row in rows:
        out["t"].append(row["ts"])
        out["engine"].append(row["engine"])
        out["ok"].append(row["ok"])
        out["down"].append(_round(row["down_mbps"], 2))
        out["up"].append(_round(row["up_mbps"], 2))
        out["ping"].append(_round(row["ping_ms"], 2))
        out["jitter"].append(_round(row["jitter_ms"], 2))
        out["loss"].append(_round(row["loss_pct"], 2))
        out["server"].append(row["server"])
        out["isp"].append(row["isp"])
        out["url"].append(row["result_url"])
        out["gb"].append(round(((row["bytes_down"] or 0) + (row["bytes_up"] or 0)) / 1e9, 3))
        out["error"].append(row["error"])
    return out


def export_lan(conn, start: int, end: int) -> dict[str, list]:
    """Local-network (Wi-Fi) tests, column-wise. Hourly at most, so never bucketed."""
    rows = conn.execute(
        """SELECT ts, ok, down_mbps, up_mbps, retransmits, band, channel,
                  rssi_dbm, rx_rate_mbps, tx_rate_mbps, error
           FROM lan WHERE ts >= ? AND ts < ? ORDER BY ts""",
        (start, end),
    ).fetchall()
    keys = {"t": "ts", "ok": "ok", "down": "down_mbps", "up": "up_mbps",
            "retrans": "retransmits", "band": "band", "channel": "channel",
            "rssi": "rssi_dbm", "rx": "rx_rate_mbps", "tx": "tx_rate_mbps", "error": "error"}
    out: dict[str, list] = {k: [] for k in keys}
    for row in rows:
        for key, column in keys.items():
            out[key].append(_round(row[column], 2))
    return out


def export_http(conn, start: int, end: int, bucket: int | None) -> dict[str, Any]:
    if bucket:
        rows = conn.execute(
            """SELECT target, (ts / :b) * :b AS bucket, AVG(ttfb_ms) AS ttfb,
                      AVG(CASE WHEN ok = 1 THEN 100.0 ELSE 0 END) AS avail
               FROM http WHERE ts >= :start AND ts < :end
               GROUP BY target, bucket ORDER BY bucket""",
            {"b": bucket, "start": start, "end": end},
        ).fetchall()
        key_t, key_v, key_a = "bucket", "ttfb", "avail"
    else:
        rows = conn.execute(
            """SELECT target, ts AS bucket, ttfb_ms AS ttfb, (ok * 100.0) AS avail
               FROM http WHERE ts >= ? AND ts < ? ORDER BY ts""",
            (start, end),
        ).fetchall()
        key_t, key_v, key_a = "bucket", "ttfb", "avail"

    series: dict[str, dict[str, list]] = {}
    for row in rows:
        entry = series.setdefault(row["target"], {"t": [], "ttfb": [], "avail": []})
        entry["t"].append(row[key_t])
        entry["ttfb"].append(_round(row[key_v], 1))
        entry["avail"].append(_round(row[key_a], 1))
    return series


def export_dns(conn, start: int, end: int, bucket: int | None) -> dict[str, Any]:
    step = bucket or 1
    rows = conn.execute(
        """SELECT target, resolver, (ts / :b) * :b AS bucket, AVG(ms) AS ms,
                  AVG(CASE WHEN ok = 1 THEN 100.0 ELSE 0 END) AS avail
           FROM dns WHERE ts >= :start AND ts < :end
           GROUP BY target, resolver, bucket ORDER BY bucket""",
        {"b": step, "start": start, "end": end},
    ).fetchall()
    series: dict[str, dict[str, list]] = {}
    for row in rows:
        key = f"{row['target']} @{row['resolver']}"
        entry = series.setdefault(key, {"t": [], "ms": [], "avail": []})
        entry["t"].append(row["bucket"])
        entry["ms"].append(_round(row["ms"], 2))
        entry["avail"].append(_round(row["avail"], 1))
    return series


# ---------------------------------------------------------------------------
# outages & summary
# ---------------------------------------------------------------------------
def find_drops(conn, cfg: Config, start: int, end: int, min_seconds: int = 60):
    """Every sweep in which all internet (non-LAN) ICMP targets were unreachable.

    Each event has a "kind":
      internet  the router still answered, so the line or the ISP failed
      local     the router was unreachable too: this machine lost its own
                network (Wi-Fi dropped, cable out, waking from sleep), which
                says nothing about the broadband
    """
    icmp = cfg.targets_with("icmp")
    wan = [t["name"] for t in icmp if t.get("group", "internet") != "lan"]
    lan = [t["name"] for t in icmp if t.get("group", "internet") == "lan"]
    if not wan:
        return []

    names = wan + lan
    placeholders = ",".join("?" for _ in names)
    lan_marks = ",".join("?" for _ in lan) or "NULL"
    rows = conn.execute(
        f"""SELECT ts,
                   SUM(CASE WHEN target IN ({lan_marks}) THEN 0 ELSE 1 END) AS n,
                   SUM(CASE WHEN target IN ({lan_marks}) THEN 0
                            WHEN loss_pct >= 100 THEN 1 ELSE 0 END) AS dead,
                   SUM(CASE WHEN target IN ({lan_marks}) THEN 1 ELSE 0 END) AS lan_n,
                   SUM(CASE WHEN target IN ({lan_marks}) AND loss_pct >= 100
                            THEN 1 ELSE 0 END) AS lan_dead
            FROM latency
            WHERE ts >= ? AND ts < ? AND target IN ({placeholders})
            GROUP BY ts ORDER BY ts""",
        [*lan, *lan, *lan, *lan, start, end, *names],
    ).fetchall()

    interval = max(1, int(cfg.get("latency.interval_seconds", 60)))
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for row in rows:
        down = row["n"] > 0 and row["dead"] == row["n"]
        kind = "local" if row["lan_n"] and row["lan_dead"] == row["lan_n"] else "internet"
        if down:
            if (current and current["kind"] == kind
                    and row["ts"] - current["end"] <= interval * 2):
                current["end"] = row["ts"]
            else:
                if current:
                    events.append(current)
                current = {"start": row["ts"], "end": row["ts"], "kind": kind}
        elif current:
            events.append(current)
            current = None
    if current:
        events.append(current)

    result = []
    for event in events:
        duration = event["end"] - event["start"] + interval
        if duration >= min_seconds:
            result.append({
                "start": event["start"], "end": event["end"] + interval,
                "seconds": duration, "kind": event["kind"],
            })
    return result


def find_outages(conn, cfg: Config, start: int, end: int, min_seconds: int = 60):
    """Internet outages only: every WAN target down while the router answered."""
    return [d for d in find_drops(conn, cfg, start, end, min_seconds) if d["kind"] == "internet"]


def window_summary(conn, cfg: Config, start: int, end: int) -> dict[str, Any]:
    speed = conn.execute(
        """SELECT down_mbps, up_mbps, ping_ms, jitter_ms, engine
           FROM speed WHERE ok = 1 AND ts >= ? AND ts < ?""",
        (start, end),
    ).fetchall()
    downs = [r["down_mbps"] for r in speed if r["down_mbps"] is not None]
    ups = [r["up_mbps"] for r in speed if r["up_mbps"] is not None]
    pings = [r["ping_ms"] for r in speed if r["ping_ms"] is not None]

    guaranteed = float(cfg.get("site.isp.guaranteed_min_down_mbps", 0) or 0)
    below = sum(1 for d in downs if guaranteed and d < guaranteed)

    primary = cfg.primary_target
    latency_stats = None
    if primary:
        row = conn.execute(
            """SELECT AVG(rtt_avg) AS avg, MAX(rtt_max) AS max, AVG(loss_pct) AS loss,
                      COUNT(*) AS n,
                      SUM(CASE WHEN loss_pct >= 100 THEN 1 ELSE 0 END) AS dead
               FROM latency WHERE target = ? AND ts >= ? AND ts < ?""",
            (primary["name"], start, end),
        ).fetchone()
        p95_rows = conn.execute(
            "SELECT rtt_avg FROM latency WHERE target = ? AND ts >= ? AND ts < ? "
            "AND rtt_avg IS NOT NULL",
            (primary["name"], start, end),
        ).fetchall()
        latency_stats = {
            "target": primary["name"],
            "avg_ms": _round(row["avg"], 2),
            "max_ms": _round(row["max"], 2),
            "p95_ms": _round(_percentile([r["rtt_avg"] for r in p95_rows], 95), 2),
            "loss_pct": _round(row["loss"], 3),
            "samples": row["n"],
            "unreachable_samples": row["dead"],
        }

    drops = find_drops(conn, cfg, start, end)
    outages = [d for d in drops if d["kind"] == "internet"]
    local = [d for d in drops if d["kind"] == "local"]
    outage_seconds = sum(o["seconds"] for o in outages)
    span = max(1, end - start)

    data = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(bytes_down,0)+COALESCE(bytes_up,0)),0) AS b "
        "FROM speed WHERE ts >= ? AND ts < ?",
        (start, end),
    ).fetchone()["b"]

    return {
        "start": start,
        "end": end,
        "tests": len(speed),
        "down": {
            "avg": _round(sum(downs) / len(downs), 2) if downs else None,
            "min": _round(min(downs), 2) if downs else None,
            "max": _round(max(downs), 2) if downs else None,
            "p10": _round(_percentile(downs, 10), 2),
            "median": _round(_percentile(downs, 50), 2),
        },
        "up": {
            "avg": _round(sum(ups) / len(ups), 2) if ups else None,
            "min": _round(min(ups), 2) if ups else None,
            "max": _round(max(ups), 2) if ups else None,
            "median": _round(_percentile(ups, 50), 2),
        },
        "ping": {
            "avg": _round(sum(pings) / len(pings), 2) if pings else None,
            "p95": _round(_percentile(pings, 95), 2),
        },
        "latency": latency_stats,
        "outages": outages[-50:],
        "outage_count": len(outages),
        "outage_seconds": outage_seconds,
        # This machine's own network was down: not counted against the line.
        "local_drops": local[-50:],
        "local_drop_count": len(local),
        "local_drop_seconds": sum(d["seconds"] for d in local),
        "availability_pct": round(100 * (1 - outage_seconds / span), 4),
        "below_guaranteed_pct": (
            round(100 * below / len(downs), 2) if downs and guaranteed else None
        ),
        "data_used_gb": round(data / 1e9, 2),
    }


# ---------------------------------------------------------------------------
# export orchestration
# ---------------------------------------------------------------------------
def collector_id(cfg: Config) -> str:
    """This collector's directory name under data/: publish.collector_id, or the
    hostname, reduced to a URL-safe slug ("Tegra Ubuntu" -> "tegra-ubuntu")."""
    raw = (cfg.get("publish.collector_id") or "").strip() or socket.gethostname().split(".")[0]
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug[:40] or "collector"


def write_collectors_index(data_root: Path) -> list[dict[str, Any]]:
    """data/collectors.json: every collector directory that holds a manifest.

    Wired collectors sort first, so the dashboard opens on the measurement that
    can be held against the line unless the visitor picked another.
    """
    entries = []
    if data_root.is_dir():
        for manifest_path in sorted(data_root.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("skipping unreadable %s", manifest_path)
                continue
            host = manifest.get("host") or {}
            entries.append({
                "id": manifest_path.parent.name,
                "label": host.get("label") or manifest_path.parent.name,
                "connection": host.get("connection"),
                "link_speed_mbps": host.get("link_speed_mbps"),
                "generated_at": manifest.get("generated_at"),
            })
    entries.sort(key=lambda e: (e["connection"] != "wired", str(e["label"]).lower()))
    _write_json(data_root / COLLECTORS_INDEX, {
        "generated_at": int(time.time()),
        "collectors": entries,
    })
    return entries


def _on_demand(cfg: Config) -> list[dict[str, Any]]:
    from .control import published_status  # control imports this module

    return published_status(cfg)


def build_export(cfg: Config, out_dir: Path) -> dict[str, Any]:
    tz = _tzinfo(cfg.get("site.timezone", "UTC"))
    now = int(time.time())
    bucket = int(cfg.get("publish.bucket_seconds", 300))
    latest_hours = int(cfg.get("publish.latest_hours", 48))
    keep_daily = int(cfg.get("publish.keep_daily_days", 400))

    data_dir = out_dir / DATA_DIRNAME / collector_id(cfg)
    if data_dir.exists():
        shutil.rmtree(data_dir)
    (data_dir / "daily").mkdir(parents=True, exist_ok=True)
    (data_dir / "monthly").mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    total_bytes = 0

    with db.connect(cfg.db_path, readonly=True) as conn:
        first = conn.execute("SELECT MIN(ts) AS ts FROM latency").fetchone()["ts"]
        first_speed = conn.execute("SELECT MIN(ts) AS ts FROM speed").fetchone()["ts"]
        first_ts = min(x for x in (first, first_speed, now) if x)

        # ---- latest.json: full resolution recent window --------------------
        latest_start = now - latest_hours * 3600
        latest = {
            "generated_at": now,
            "from": latest_start,
            "to": now,
            "resolution": int(cfg.get("latency.interval_seconds", 60)),
            "latency": export_latency(conn, latest_start, now + 1, None),
            "speed": export_speed(conn, latest_start, now + 1),
            "lan": export_lan(conn, latest_start, now + 1),
            "http": export_http(conn, latest_start, now + 1, None),
            "dns": export_dns(conn, latest_start, now + 1, None),
        }
        total_bytes += _write_json(data_dir / "latest.json", latest)
        written.append("latest.json")

        # ---- daily files ---------------------------------------------------
        today = dt.datetime.fromtimestamp(now, tz).date()
        oldest = dt.datetime.fromtimestamp(first_ts, tz).date()
        cutoff = today - dt.timedelta(days=keep_daily)
        day = max(oldest, cutoff)
        days: list[str] = []

        while day <= today:
            start, end = _day_bounds(day, tz)
            latency = export_latency(conn, start, end, bucket)
            speed = export_speed(conn, start, end)
            if latency or speed["t"]:
                payload = {
                    "date": day.isoformat(),
                    "from": start,
                    "to": end,
                    "resolution": bucket,
                    "latency": latency,
                    "speed": speed,
                    "lan": export_lan(conn, start, end),
                    "http": export_http(conn, start, end, bucket),
                    "dns": export_dns(conn, start, end, bucket),
                    "summary": window_summary(conn, cfg, start, end),
                }
                total_bytes += _write_json(data_dir / "daily" / f"{day.isoformat()}.json", payload)
                days.append(day.isoformat())
            day += dt.timedelta(days=1)

        # ---- monthly rollups (hourly resolution) ---------------------------
        months: list[str] = []
        month = dt.date(oldest.year, oldest.month, 1)
        while month <= today:
            next_month = dt.date(
                month.year + (month.month // 12), (month.month % 12) + 1, 1
            )
            start, end = _day_bounds(month, tz)[0], _day_bounds(next_month, tz)[0]
            latency = export_latency(conn, start, end, 3600)
            speed = export_speed(conn, start, end)
            if latency or speed["t"]:
                payload = {
                    "month": month.strftime("%Y-%m"),
                    "from": start,
                    "to": end,
                    "resolution": 3600,
                    "latency": latency,
                    "speed": speed,
                    "lan": export_lan(conn, start, end),
                    "summary": window_summary(conn, cfg, start, end),
                }
                total_bytes += _write_json(
                    data_dir / "monthly" / f"{month.strftime('%Y-%m')}.json", payload
                )
                months.append(month.strftime("%Y-%m"))
            month = next_month

        # ---- summary.json --------------------------------------------------
        windows = {
            "24h": window_summary(conn, cfg, now - 86400, now + 1),
            "7d": window_summary(conn, cfg, now - 7 * 86400, now + 1),
            "30d": window_summary(conn, cfg, now - 30 * 86400, now + 1),
            "all": window_summary(conn, cfg, first_ts, now + 1),
        }
        last_speed = conn.execute(
            "SELECT * FROM speed WHERE ok = 1 ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        detected_isp = last_speed["isp"] if last_speed else None

        summary = {
            "generated_at": now,
            "windows": windows,
            "current": dict(last_speed) if last_speed else None,
            "engines": cfg.get("speed.engines", []),
        }
        if summary["current"]:
            summary["current"].pop("raw", None)
        total_bytes += _write_json(data_dir / "summary.json", summary)
        written.append("summary.json")

    isp = dict(cfg.get("site.isp", {}))
    if not isp.get("name") and detected_isp:
        isp["name"] = detected_isp

    manifest = {
        "version": __version__,
        "id": collector_id(cfg),
        "generated_at": now,
        "site": {
            "title": cfg.get("site.title"),
            "subtitle": cfg.get("site.subtitle"),
            "timezone": cfg.get("site.timezone"),
            "isp": isp,
        },
        "host": _host(cfg),
        "collection": {
            "latency_interval_seconds": int(cfg.get("latency.interval_seconds", 60)),
            "speed_interval_minutes": int(cfg.get("speed.interval_minutes", 30)),
            "bucket_seconds": bucket,
            "latest_hours": latest_hours,
        },
        "targets": [
            {
                "name": t["name"],
                "host": t["host"],
                "group": t.get("group", "internet"),
                "checks": t.get("checks", ["icmp"]),
                "primary": bool(t.get("primary")),
            }
            for t in cfg.targets
        ],
        "days": days,
        "months": months,
        "first_sample": first_ts,
        "control": control_info(cfg),
        "lan": {"server": cfg.get("lan.server")} if cfg.get("lan.enabled") else None,
        "on_demand": _on_demand(cfg),
    }
    total_bytes += _write_json(data_dir / "manifest.json", manifest)
    written.append("manifest.json")

    log.info(
        "exported %d day files, %d month files, %.1f kB total",
        len(days), len(months), total_bytes / 1024,
    )
    return {"days": days, "months": months, "bytes": total_bytes, "dir": data_dir}


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------
def git_ssh_command(key: str, known_hosts: str | None = None) -> str:
    """GIT_SSH_COMMAND that pins one deploy key and never prompts.

    Forward slashes on purpose: Git for Windows splits this string with a POSIX
    shell, which would eat the backslashes of C:\\ProgramData\\... paths.
    """
    key_s = str(key).replace("\\", "/")
    known = str(known_hosts).replace("\\", "/") if known_hosts else (
        key_s.rsplit("/", 1)[0] + "/known_hosts" if "/" in key_s else "known_hosts"
    )
    return (
        f'ssh -i "{key_s}" -o IdentitiesOnly=yes '
        f'-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="{known}"'
    )


def git_env(cfg: Config) -> dict[str, str] | None:
    """Environment for git calls that reach the remote.

    The systemd units set GIT_SSH_COMMAND themselves; publish.ssh_key exists for
    schedulers that cannot (Windows Task Scheduler) and overrides it if set.
    """
    if cfg.get("publish.ssh_key"):
        return {**os.environ, "GIT_SSH_COMMAND": git_ssh_command(cfg.get("publish.ssh_key"))}
    return None


def github_repo(remote: str) -> str | None:
    """Return "owner/repo" for a GitHub SSH or HTTPS remote, None for anything else."""
    match = re.match(
        r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
        r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
        (remote or "").strip(),
    )
    return match.group(1) if match else None


def control_info(cfg: Config) -> dict[str, Any] | None:
    """What the dashboard needs to offer on-demand tests for this collector."""
    repo = github_repo(cfg.get("publish.remote") or "")
    if not cfg.get("control.enabled") or not repo:
        return None
    return {
        "repo": repo,
        "workflow": cfg.get("control.workflow", "run-test.yml"),
        "ref": cfg.get("control.ref", "main"),
        "engines": list(cfg.get("speed.engines", [])),
    }


def _git(
    args: list[str], cwd: Path, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    # Trust this one repo whoever owns it. The scratch repo may have been made
    # by a manual run as a person and then used by SYSTEM / the service user
    # (or the reverse), and git refuses that as "dubious ownership". A -c on
    # the command line counts as protected config, so git honours it.
    safe = f"safe.directory={Path(cwd).resolve().as_posix()}"
    proc = subprocess.run(
        [GIT, "-c", safe, *args], cwd=cwd,
        capture_output=True, text=True, check=False, timeout=300,
        env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:400]}"
        )
    return proc


def copy_contents(src: Path, dst: Path) -> None:
    """Recursively copy file *contents* — never permissions, owner or times.

    shutil.copytree replicates directory metadata onto the destination, which
    fails with EPERM when the source carries modes the copying user cannot
    reproduce — a setgid directory in someone's home, say, which is exactly how
    an --in-place checkout is set up so two accounts can share the git repo.
    None of that metadata means anything in the publisher's scratch area or on
    a git branch, so simply do not copy it.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            copy_contents(item, target)
        else:
            shutil.copyfile(item, target)


def _sync_tree(src: Path, dst: Path) -> None:
    """Mirror src into dst, leaving dst/.git alone."""
    for item in dst.iterdir():
        if item.name == ".git":
            continue
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            copy_contents(item, target)
        else:
            shutil.copyfile(item, target)


def _fetch_published(repo: Path, branch: str, env: dict[str, str] | None) -> str | None:
    """Fetch the live branch; return its commit, or None if it does not exist yet."""
    ref = f"refs/remotes/origin/{branch}"
    fetch = _git(
        ["fetch", "-q", "--depth", "1", "origin", f"+refs/heads/{branch}:{ref}"],
        repo, check=False, env=env,
    )
    if fetch.returncode != 0:
        err = (fetch.stderr or "").lower()
        if "couldn't find remote ref" in err or "could not find remote ref" in err:
            return None
        raise RuntimeError(f"git fetch {branch} failed: {(fetch.stderr or '').strip()[:400]}")
    return _git(["rev-parse", ref], repo).stdout.strip()


def carry_other_collectors(
    repo: Path, base: str, own_id: str, drop: set[str], keep_cname: bool
) -> list[str]:
    """Copy every other collector's data/<id>/ from the live commit into repo.

    Only directories with a manifest count: the pre-multi-collector layout kept
    its files straight under data/, and those are left behind on purpose.
    """
    listing = _git(["ls-tree", "--name-only", f"{base}:{DATA_DIRNAME}"], repo, check=False)
    kept = []
    for name in listing.stdout.split():
        if name == own_id or name in drop:
            continue
        probe = _git(
            ["cat-file", "-e", f"{base}:{DATA_DIRNAME}/{name}/manifest.json"], repo, check=False
        )
        if probe.returncode == 0:
            kept.append(name)
    paths = [f"{DATA_DIRNAME}/{name}" for name in kept]
    if keep_cname and _git(["cat-file", "-e", f"{base}:CNAME"], repo, check=False).returncode == 0:
        paths.append("CNAME")  # a collector with no site.domain must not unset it
    if paths:
        _git(["checkout", base, "--", *paths], repo)
    return kept


def _lost_race(proc: subprocess.CompletedProcess) -> bool:
    err = (proc.stderr or "").lower()
    return any(s in err for s in ("stale info", "rejected", "fetch first", "non-fast-forward"))


def push(cfg: Config, tree: Path) -> None:
    work = Path(cfg.get("publish.work_dir"))
    repo = work / "repo"
    branch = cfg.get("publish.branch", "gh-pages")
    remote = cfg.get("publish.remote")
    squash = bool(cfg.get("publish.squash", True))
    own_id = collector_id(cfg)
    drop = {str(x) for x in (cfg.get("publish.drop_collectors") or [])}
    keep_cname = not (cfg.get("site.domain") or "").strip()

    env = git_env(cfg)

    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        log.info("initialising publish repo at %s", repo)
        _git(["init", "-q", "-b", branch], repo)
        _git(["remote", "add", "origin", remote], repo)
    else:
        _git(["remote", "set-url", "origin", remote], repo)

    _git(["config", "user.name", cfg.get("publish.git_user_name")], repo)
    _git(["config", "user.email", cfg.get("publish.git_user_email")], repo)

    for attempt in range(1, PUSH_ATTEMPTS + 1):
        base = _fetch_published(repo, branch, env)
        _sync_tree(tree, repo)
        others = carry_other_collectors(repo, base, own_id, drop, keep_cname) if base else []
        write_collectors_index(repo / DATA_DIRNAME)

        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        message = f"data: {own_id} snapshot {stamp}"

        if squash:
            # One parentless commit, built with plumbing so no checkout state can
            # get in the way of a retry.
            _git(["add", "-A"], repo)
            tree_sha = _git(["write-tree"], repo).stdout.strip()
            commit = _git(["commit-tree", tree_sha, "-m", message], repo).stdout.strip()
            _git(["update-ref", f"refs/heads/{branch}", commit], repo)
            _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], repo)
            proc = _git(
                ["push", f"--force-with-lease={branch}:{base or ''}", "origin", branch],
                repo, check=False, env=env,
            )
        else:
            if base:
                _git(["reset", "-q", "--mixed", base], repo)
            _git(["add", "-A"], repo)
            if not _git(["status", "--porcelain"], repo).stdout.strip():
                log.info("no changes to publish")
                return
            _git(["commit", "-q", "-m", message], repo)
            proc = _git(["push", "origin", f"HEAD:{branch}"], repo, check=False, env=env)

        if proc.returncode == 0:
            log.info(
                "pushed %s to %s as %s%s", branch, remote, own_id,
                f" (alongside {', '.join(others)})" if others else "",
            )
            return
        if not _lost_race(proc) or attempt == PUSH_ATTEMPTS:
            raise RuntimeError(
                f"git push failed ({proc.returncode}): {(proc.stderr or '').strip()[:400]}"
            )
        wait = random.uniform(2, 10) * attempt
        log.info("another collector published first; retrying in %.0fs", wait)
        time.sleep(wait)


def _host(cfg: Config) -> dict[str, Any]:
    """What the dashboard says collected the data: the board and its hostname,
    unless site.collector overrides it (say, to keep the hostname private)."""
    host = dict(link.describe())
    override = (cfg.get("site.collector") or "").strip()
    host["label"] = override or f"{host['model']} ({host['hostname']})"
    return host


def owned_by_someone_else(path: Path) -> bool:
    """True when path's owner is not the running user (POSIX only)."""
    if not hasattr(os, "geteuid"):
        return False  # Windows: ACLs inherit, and SYSTEM may pull the clone
    try:
        return path.stat().st_uid != os.geteuid()
    except OSError:
        return False


# ---------------------------------------------------------------------------
def publish(cfg: Config, dry_run: bool = False, export_only: bool = False) -> int:
    repo_dir = Path(cfg.get("publish.repo_dir"))
    site_src = repo_dir / "site"
    if not site_src.is_dir():
        # running straight from a git checkout
        site_src = Path(__file__).resolve().parent.parent / "site"
    if not site_src.is_dir():
        log.error("site directory not found (looked in %s)", site_src)
        return 1

    if export_only:
        result = build_export(cfg, site_src)
        write_collectors_index(site_src / DATA_DIRNAME)
        log.info("exported into %s", result["dir"])
        return 0

    global GIT
    GIT = find_git(cfg.get("publish.git_binary") or "")
    log.info("publishing as %s (git: %s)", collector_id(cfg), GIT)
    if cfg.get("publish.sync_code") and (repo_dir / ".git").is_dir():
        if owned_by_someone_else(repo_dir):
            # An --in-place checkout belongs to the human who edits it. A pull by
            # the service user leaves files in .git the human cannot overwrite
            # (FETCH_HEAD, new object dirs) and fails on their uncommitted work;
            # their own `git pull` is the update path there.
            log.info("code sync skipped: %s belongs to another user", repo_dir)
        else:
            # Never wait on a credential prompt nobody can answer (SYSTEM, cron).
            pull = _git(["pull", "--ff-only", "-q"], repo_dir, check=False,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
            if pull.returncode != 0:
                log.warning("code sync skipped: %s", (pull.stderr or "").strip()[:200])

    work = Path(cfg.get("publish.work_dir"))
    tree = work / "tree"
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True, exist_ok=True)

    for item in site_src.iterdir():
        if item.name == DATA_DIRNAME:
            continue
        if item.is_dir():
            copy_contents(item, tree / item.name)
        else:
            shutil.copyfile(item, tree / item.name)

    build_export(cfg, tree)
    write_collectors_index(tree / DATA_DIRNAME)

    (tree / ".nojekyll").write_text("", encoding="utf-8")
    domain = (cfg.get("site.domain") or "").strip()
    if domain:
        (tree / "CNAME").write_text(domain + "\n", encoding="utf-8")

    if dry_run:
        log.info("dry run — built site tree at %s, not pushing", tree)
        return 0

    if not cfg.get("publish.enabled"):
        log.info("publish.enabled is false — built %s but not pushing", tree)
        return 0

    try:
        push(cfg, tree)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    return 0
