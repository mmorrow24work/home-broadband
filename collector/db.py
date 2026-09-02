"""SQLite storage. The Pi's database is the system of record; published JSON
is a derived, disposable view of it."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS latency (
    ts        INTEGER NOT NULL,
    target    TEXT    NOT NULL,
    host      TEXT    NOT NULL,
    family    TEXT    NOT NULL,
    sent      INTEGER NOT NULL,
    recv      INTEGER NOT NULL,
    loss_pct  REAL    NOT NULL,
    rtt_min   REAL,
    rtt_avg   REAL,
    rtt_max   REAL,
    rtt_mdev  REAL,
    error     TEXT,
    PRIMARY KEY (ts, target)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_latency_ts ON latency(ts);

CREATE TABLE IF NOT EXISTS dns (
    ts       INTEGER NOT NULL,
    target   TEXT    NOT NULL,
    host     TEXT    NOT NULL,
    resolver TEXT    NOT NULL,
    ok       INTEGER NOT NULL,
    ms       REAL,
    answer   TEXT,
    error    TEXT,
    PRIMARY KEY (ts, target, resolver)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_dns_ts ON dns(ts);

CREATE TABLE IF NOT EXISTS http (
    ts          INTEGER NOT NULL,
    target      TEXT    NOT NULL,
    url         TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    status      INTEGER,
    dns_ms      REAL,
    connect_ms  REAL,
    tls_ms      REAL,
    ttfb_ms     REAL,
    total_ms    REAL,
    error       TEXT,
    PRIMARY KEY (ts, target)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_http_ts ON http(ts);

CREATE TABLE IF NOT EXISTS speed (
    ts           INTEGER NOT NULL PRIMARY KEY,
    engine       TEXT    NOT NULL,
    ok           INTEGER NOT NULL,
    down_mbps    REAL,
    up_mbps      REAL,
    ping_ms      REAL,
    jitter_ms    REAL,
    loss_pct     REAL,
    server       TEXT,
    server_id    TEXT,
    isp          TEXT,
    external_ip  TEXT,
    result_url   TEXT,
    bytes_down   INTEGER,
    bytes_up     INTEGER,
    duration_s   REAL,
    error        TEXT,
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_speed_engine ON speed(engine, ts);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


@contextmanager
def connect(path: str | Path, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init(path: str | Path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO meta(k, v) VALUES('schema_version', ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(SCHEMA_VERSION),),
        )


# -- meta helpers ---------------------------------------------------------
def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )


# -- writers --------------------------------------------------------------
def insert_latency(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO latency
           (ts, target, host, family, sent, recv, loss_pct,
            rtt_min, rtt_avg, rtt_max, rtt_mdev, error)
           VALUES (:ts, :target, :host, :family, :sent, :recv, :loss_pct,
                   :rtt_min, :rtt_avg, :rtt_max, :rtt_mdev, :error)""",
        rows,
    )


def insert_dns(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO dns (ts, target, host, resolver, ok, ms, answer, error)
           VALUES (:ts, :target, :host, :resolver, :ok, :ms, :answer, :error)""",
        rows,
    )


def insert_http(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO http
           (ts, target, url, ok, status, dns_ms, connect_ms, tls_ms, ttfb_ms, total_ms, error)
           VALUES (:ts, :target, :url, :ok, :status, :dns_ms, :connect_ms, :tls_ms,
                   :ttfb_ms, :total_ms, :error)""",
        rows,
    )


def insert_speed(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    payload = dict(row)
    if isinstance(payload.get("raw"), (dict, list)):
        payload["raw"] = json.dumps(payload["raw"], separators=(",", ":"))
    conn.execute(
        """INSERT OR REPLACE INTO speed
           (ts, engine, ok, down_mbps, up_mbps, ping_ms, jitter_ms, loss_pct,
            server, server_id, isp, external_ip, result_url,
            bytes_down, bytes_up, duration_s, error, raw)
           VALUES (:ts, :engine, :ok, :down_mbps, :up_mbps, :ping_ms, :jitter_ms, :loss_pct,
                   :server, :server_id, :isp, :external_ip, :result_url,
                   :bytes_down, :bytes_up, :duration_s, :error, :raw)""",
        payload,
    )


# -- readers --------------------------------------------------------------
def next_engine(conn: sqlite3.Connection, engines: list[str]) -> str:
    """Round-robin the configured engines based on the last successful run."""
    if len(engines) == 1:
        return engines[0]
    row = conn.execute("SELECT engine FROM speed ORDER BY ts DESC LIMIT 1").fetchone()
    if row is None or row["engine"] not in engines:
        return engines[0]
    return engines[(engines.index(row["engine"]) + 1) % len(engines)]


def bytes_used_since(conn: sqlite3.Connection, since_ts: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(bytes_down,0) + COALESCE(bytes_up,0)), 0) AS total "
        "FROM speed WHERE ts >= ?",
        (since_ts,),
    ).fetchone()
    return int(row["total"])


def prune(conn: sqlite3.Connection, retention_days: int) -> dict[str, int]:
    """Delete raw samples older than the retention window."""
    if not retention_days:
        return {}
    cutoff = int(time.time()) - retention_days * 86400
    deleted = {}
    for table in ("latency", "dns", "http", "speed"):
        cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        deleted[table] = cur.rowcount
    return deleted
