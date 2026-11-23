#!/usr/bin/env python3
"""Bring measurement history from an older collector into this one's database.

Moving the monitor to new hardware (Pi -> Jetson, say) starts a fresh SQLite
file, and the first publish force-pushes gh-pages, so the dashboard forgets
everything before the move. Two ways to get it back:

  --from-db PATH     the old collector's broadband.db (best: every raw sample)
  --from-git REF     a published gh-pages snapshot (5-minute buckets for
  --from-dir DIR     latency/DNS/HTTP, every speed test exactly) — a commit,
                     tag or branch in this repo, or a checked-out data/ tree

Existing rows always win (INSERT OR IGNORE), so it is safe to re-run and never
overwrites what this collector measured itself. Stop the timers first so the
database is not written to mid-import:

  sudo systemctl stop 'broadband-*.timer'
  sudo -u broadband .venv/bin/python scripts/import_history.py \\
       --from-git pi-archive-2026-09-16 --db /var/lib/broadband-monitor/broadband.db
  sudo systemctl start broadband-latency.timer broadband-speed.timer \\
       broadband-publish.timer broadband-prune.timer
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import db  # noqa: E402

TABLES = ("latency", "dns", "http", "speed")


# ---------------------------------------------------------------- sources
def _git_files(ref: str, repo: Path) -> Iterator[tuple[str, dict]]:
    names = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref, "data/daily"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for name in sorted(names):
        if name.endswith(".json"):
            blob = subprocess.run(
                ["git", "-C", str(repo), "show", f"{ref}:{name}"],
                capture_output=True, text=True, check=True,
            ).stdout
            yield name, json.loads(blob)


def _dir_files(root: Path) -> Iterator[tuple[str, dict]]:
    daily = root / "daily" if (root / "daily").is_dir() else root / "data" / "daily"
    for path in sorted(daily.glob("*.json")):
        yield str(path), json.loads(path.read_text(encoding="utf-8"))


def _hosts(manifest: dict | None) -> dict[str, str]:
    return {t["name"]: t.get("host", "") for t in (manifest or {}).get("targets", [])}


def rows_from_daily(day: dict, hosts: dict[str, str]) -> dict[str, list[dict]]:
    """Turn one published daily file back into table rows.

    Buckets become one row each, stamped at the bucket start. Loss and
    availability are percentages of the bucket, so they survive exactly; RTT is
    the bucket mean, with its max kept and min/mdev unknown.
    """
    out: dict[str, list[dict]] = {t: [] for t in TABLES}

    for target, s in (day.get("latency") or {}).items():
        for i, ts in enumerate(s.get("t", [])):
            sent = int((s.get("n") or [5] * len(s["t"]))[i] or 5)
            loss = s["loss"][i]
            loss = 100.0 if loss is None else float(loss)
            out["latency"].append({
                "ts": int(ts), "target": target, "host": hosts.get(target, ""),
                "family": "ipv6" if ":" in hosts.get(target, "") else "ipv4",
                "sent": sent, "recv": round(sent * (100 - loss) / 100), "loss_pct": loss,
                "rtt_min": None, "rtt_avg": s["rtt"][i],
                "rtt_max": (s.get("rtt_max") or [None] * len(s["t"]))[i],
                "rtt_mdev": None, "error": "imported from published snapshot",
            })

    for key, s in (day.get("dns") or {}).items():
        target, _, resolver = key.rpartition(" @")
        for i, ts in enumerate(s.get("t", [])):
            ok = (s["avail"][i] or 0) >= 50
            out["dns"].append({
                "ts": int(ts), "target": target or key, "host": hosts.get(target, ""),
                "resolver": resolver or "system", "ok": int(ok),
                "ms": s["ms"][i] if ok else None, "answer": None,
                "error": None if ok else "imported: bucket mostly failed",
            })

    for target, s in (day.get("http") or {}).items():
        for i, ts in enumerate(s.get("t", [])):
            ok = (s["avail"][i] or 0) >= 50
            out["http"].append({
                "ts": int(ts), "target": target, "url": "", "ok": int(ok), "status": None,
                "dns_ms": None, "connect_ms": None, "tls_ms": None,
                "ttfb_ms": s["ttfb"][i] if ok else None, "total_ms": None,
                "error": None if ok else "imported: bucket mostly failed",
            })

    sp = day.get("speed") or {}
    for i, ts in enumerate(sp.get("t", [])):
        def col(name, i=i):
            values = sp.get(name) or []
            return values[i] if i < len(values) else None

        gb = col("gb") or 0
        out["speed"].append({
            "ts": int(ts), "engine": col("engine") or "unknown", "ok": int(col("ok") or 0),
            "down_mbps": col("down"), "up_mbps": col("up"), "ping_ms": col("ping"),
            "jitter_ms": col("jitter"), "loss_pct": col("loss"), "server": col("server"),
            "server_id": None, "isp": col("isp"), "external_ip": None,
            "result_url": col("url"), "bytes_down": int(gb * 1e9), "bytes_up": 0,
            "duration_s": None, "error": col("error"), "raw": None,
        })
    return out


# ------------------------------------------------------------------ write
def insert_ignore(conn: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = list(rows[0])
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join(':' + c for c in cols)})",
        rows,
    )
    return conn.total_changes - before


def merge_db(conn: sqlite3.Connection, source: Path) -> dict[str, int]:
    # as_uri(): file:///C:/... on Windows; a bare file:C:\... is not a valid URI.
    conn.execute("ATTACH DATABASE ? AS old", (f"{Path(source).resolve().as_uri()}?mode=ro",))
    counts = {}
    try:
        for table in TABLES:
            cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")]
            have = {r[1] for r in conn.execute(f"PRAGMA old.table_info({table})")}
            shared = ", ".join(c for c in cols if c in have)
            before = conn.total_changes
            conn.execute(
                f"INSERT OR IGNORE INTO main.{table} ({shared}) SELECT {shared} FROM old.{table}"
            )
            counts[table] = conn.total_changes - before
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE old")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-db", type=Path, help="another collector's broadband.db")
    src.add_argument("--from-git", metavar="REF", help="gh-pages commit/tag in this repo")
    src.add_argument("--from-dir", type=Path, help="a published data/ directory")
    parser.add_argument("--db", type=Path, required=True, help="database to import into")
    parser.add_argument("--dry-run", action="store_true", help="count rows, write nothing")
    args = parser.parse_args(argv)

    db.init(args.db)
    conn = sqlite3.connect(args.db, uri=True)
    conn.row_factory = sqlite3.Row

    if args.from_db:
        if args.dry_run:
            print("--dry-run is not supported with --from-db")
            return 2
        counts = merge_db(conn, args.from_db.resolve())
    else:
        repo = Path(__file__).resolve().parent.parent
        if args.from_git:
            manifest = json.loads(subprocess.run(
                ["git", "-C", str(repo), "show", f"{args.from_git}:data/manifest.json"],
                capture_output=True, text=True, check=True,
            ).stdout)
            files = _git_files(args.from_git, repo)
        else:
            mpath = next(p for p in (args.from_dir / "manifest.json",
                                     args.from_dir / "data" / "manifest.json") if p.exists())
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
            files = _dir_files(args.from_dir)

        hosts = _hosts(manifest)
        counts = dict.fromkeys(TABLES, 0)
        for name, day in files:
            rows = rows_from_daily(day, hosts)
            if args.dry_run:
                for table in TABLES:
                    counts[table] += len(rows[table])
            else:
                for table in TABLES:
                    counts[table] += insert_ignore(conn, table, rows[table])
            print(f"  {Path(name).name}: {sum(len(v) for v in rows.values())} rows")
        if not args.dry_run:
            conn.commit()

    verb = "would import" if args.dry_run else "imported"
    print(f"{verb}: " + ", ".join(f"{t}={n}" for t, n in counts.items()))
    print("Run a publish to see it on the dashboard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
