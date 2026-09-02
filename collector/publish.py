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
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__, db, link
from .config import Config

log = logging.getLogger("broadband.publish")

DATA_DIRNAME = "data"


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
def find_outages(conn, cfg: Config, start: int, end: int, min_seconds: int = 60):
    """A WAN outage = every non-LAN ICMP target unreachable in the same sweep."""
    wan_targets = [
        t["name"]
        for t in cfg.targets_with("icmp")
        if t.get("group", "internet") != "lan"
    ]
    if not wan_targets:
        return []

    placeholders = ",".join("?" for _ in wan_targets)
    rows = conn.execute(
        f"""SELECT ts, COUNT(*) AS n,
                   SUM(CASE WHEN loss_pct >= 100 THEN 1 ELSE 0 END) AS dead
            FROM latency
            WHERE ts >= ? AND ts < ? AND target IN ({placeholders})
            GROUP BY ts ORDER BY ts""",
        [start, end, *wan_targets],
    ).fetchall()

    interval = max(1, int(cfg.get("latency.interval_seconds", 60)))
    outages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for row in rows:
        down = row["n"] > 0 and row["dead"] == row["n"]
        if down:
            if current and row["ts"] - current["end"] <= interval * 2:
                current["end"] = row["ts"]
            else:
                if current:
                    outages.append(current)
                current = {"start": row["ts"], "end": row["ts"]}
        elif current:
            outages.append(current)
            current = None
    if current:
        outages.append(current)

    result = []
    for outage in outages:
        duration = outage["end"] - outage["start"] + interval
        if duration >= min_seconds:
            result.append(
                {"start": outage["start"], "end": outage["end"] + interval, "seconds": duration}
            )
    return result


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

    outages = find_outages(conn, cfg, start, end)
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
        "availability_pct": round(100 * (1 - outage_seconds / span), 4),
        "below_guaranteed_pct": (
            round(100 * below / len(downs), 2) if downs and guaranteed else None
        ),
        "data_used_gb": round(data / 1e9, 2),
    }


# ---------------------------------------------------------------------------
# export orchestration
# ---------------------------------------------------------------------------
def build_export(cfg: Config, out_dir: Path) -> dict[str, Any]:
    tz = _tzinfo(cfg.get("site.timezone", "UTC"))
    now = int(time.time())
    bucket = int(cfg.get("publish.bucket_seconds", 300))
    latest_hours = int(cfg.get("publish.latest_hours", 48))
    keep_daily = int(cfg.get("publish.keep_daily_days", 400))

    data_dir = out_dir / DATA_DIRNAME
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
        "generated_at": now,
        "site": {
            "title": cfg.get("site.title"),
            "subtitle": cfg.get("site.subtitle"),
            "timezone": cfg.get("site.timezone"),
            "isp": isp,
        },
        "host": link.describe(),
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
    }
    total_bytes += _write_json(data_dir / "manifest.json", manifest)
    written.append("manifest.json")

    log.info(
        "exported %d day files, %d month files, %.1f kB total",
        len(days), len(months), total_bytes / 1024,
    )
    return {"days": days, "months": months, "bytes": total_bytes}


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------
def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=300
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


def push(cfg: Config, tree: Path) -> None:
    work = Path(cfg.get("publish.work_dir"))
    repo = work / "repo"
    branch = cfg.get("publish.branch", "gh-pages")
    remote = cfg.get("publish.remote")
    squash = bool(cfg.get("publish.squash", True))

    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        log.info("initialising publish repo at %s", repo)
        _git(["init", "-q", "-b", branch], repo)
        _git(["remote", "add", "origin", remote], repo)
        if not squash:
            fetch = _git(["fetch", "--depth", "1", "origin", branch], repo, check=False)
            if fetch.returncode == 0:
                _git(["reset", "--hard", f"origin/{branch}"], repo)
    else:
        _git(["remote", "set-url", "origin", remote], repo)

    _git(["config", "user.name", cfg.get("publish.git_user_name")], repo)
    _git(["config", "user.email", cfg.get("publish.git_user_email")], repo)

    _sync_tree(tree, repo)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message = f"data: broadband snapshot {stamp}"

    if squash:
        _git(["checkout", "-q", "--orphan", "__publish"], repo)
        _git(["add", "-A"], repo)
        _git(["commit", "-q", "-m", message], repo)
        _git(["branch", "-M", branch], repo)
        _git(["push", "--force", "origin", branch], repo)
    else:
        _git(["add", "-A"], repo)
        status = _git(["status", "--porcelain"], repo)
        if not status.stdout.strip():
            log.info("no changes to publish")
            return
        _git(["commit", "-q", "-m", message], repo)
        _git(["push", "origin", f"HEAD:{branch}"], repo)

    log.info("pushed %s to %s", branch, remote)


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
        build_export(cfg, site_src)
        log.info("exported into %s", site_src / DATA_DIRNAME)
        return 0

    if cfg.get("publish.sync_code") and (repo_dir / ".git").is_dir():
        pull = _git(["pull", "--ff-only", "-q"], repo_dir, check=False)
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
