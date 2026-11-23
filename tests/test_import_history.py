"""Moving to new hardware must not cost the history (Pi -> Jetson, 2026-11)."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("import_history", ROOT / "scripts/import_history.py")
ih = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ih)

DAY = {
    "latency": {"Router": {"t": [100, 400], "rtt": [1.0, None], "rtt_max": [1.3, None],
                           "loss": [20.0, 100.0], "n": [5, 5]}},
    "dns": {"BBC @1.1.1.1": {"t": [100], "ms": [12.0], "avail": [100.0]}},
    "http": {"BBC": {"t": [100], "ttfb": [80.0], "avail": [0.0]}},
    "speed": {"t": [130], "engine": ["cloudflare"], "ok": [1], "down": [92.2], "up": [92.3],
              "ping": [37.0], "jitter": [12.0], "loss": [0.0], "server": ["LHR"],
              "isp": [None], "url": [None], "gb": [0.26]},
}


def test_daily_buckets_become_rows():
    rows = ih.rows_from_daily(DAY, {"Router": "192.168.1.1"})
    lat = rows["latency"]
    assert [r["recv"] for r in lat] == [4, 0]
    assert lat[0]["host"] == "192.168.1.1" and lat[1]["rtt_avg"] is None
    assert rows["dns"][0]["target"] == "BBC" and rows["dns"][0]["resolver"] == "1.1.1.1"
    assert rows["http"][0]["ok"] == 0 and rows["http"][0]["ttfb_ms"] is None
    assert rows["speed"][0]["down_mbps"] == 92.2 and rows["speed"][0]["bytes_down"] == 260_000_000


def test_import_never_overwrites_and_is_rerunnable(tmp_path):
    from collector import db

    path = tmp_path / "b.db"
    db.init(path)
    conn = sqlite3.connect(path)
    rows = ih.rows_from_daily(DAY, {})
    mine = dict(rows["speed"][0], down_mbps=500.0)  # this collector's own row wins
    db.insert_speed(conn, mine)
    conn.commit()

    assert ih.insert_ignore(conn, "speed", rows["speed"]) == 0
    assert ih.insert_ignore(conn, "latency", rows["latency"]) == 2
    assert ih.insert_ignore(conn, "latency", rows["latency"]) == 0
    assert conn.execute("SELECT down_mbps FROM speed").fetchone()[0] == 500.0


def test_merge_from_another_database(tmp_path):
    from collector import db

    old, new = tmp_path / "pi.db", tmp_path / "jetson.db"
    for p in (old, new):
        db.init(p)
    c = sqlite3.connect(old)
    ih.insert_ignore(c, "latency", ih.rows_from_daily(DAY, {})["latency"])
    c.commit()
    c.close()

    conn = sqlite3.connect(new)
    assert ih.merge_db(conn, old)["latency"] == 2
    assert ih.merge_db(conn, old)["latency"] == 0
