#!/usr/bin/env python3
"""Fill a throwaway database with plausible data and export the site.

Useful for previewing the dashboard before the Pi has collected anything real,
and for working on the front end without waiting 24 hours for a chart to fill.

    python3 scripts/seed_demo.py --days 10 --serve

Then open http://localhost:8000/ — nothing here touches your live database
unless you point --db at it.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import db  # noqa: E402
from collector.config import load_config  # noqa: E402
from collector.publish import build_export  # noqa: E402

# Per-engine behaviour, so the demo shows what the real thing shows: the engines
# do not agree, and only Ookla produces a shareable result URL.
ENGINE_PROFILES = {
    "ookla": {
        "bias": 1.0, "jitter": True, "server": "London", "server_id": "12345",
        "url": "https://www.speedtest.net/result/c/demo",
        "bytes_down": 620_000_000, "bytes_up": 95_000_000, "duration": 24.0,
    },
    "speedtest-cli": {
        "bias": 0.86, "jitter": False, "server": "Faelix, London", "server_id": "12345",
        "url": None,
        "bytes_down": 540_000_000, "bytes_up": 90_000_000, "duration": 38.0,
    },
    "cloudflare": {
        "bias": 0.97, "jitter": True, "server": "London, LHR", "server_id": "LHR",
        "url": None,
        "bytes_down": 250_000_000, "bytes_up": 100_000_000, "duration": 26.0,
    },
}

BASE_RTT = {"Router": 0.7, "ISP gateway": 8.5, "Google DNS": 11.0, "Quad9": 13.5,
            "Cloudflare v6": 9.2, "BBC": 12.5}


def seed(cfg, days: int, seed_value: int = 7) -> None:
    rng = random.Random(seed_value)
    now = int(time.time())
    interval = int(cfg.get("latency.interval_seconds", 60))
    icmp = [t["name"] for t in cfg.targets_with("icmp")]
    http_targets = list(cfg.targets_with("http"))

    # two outages: a short blip and a longer evening drop
    outages = {
        now - int(2.5 * 86400): 6 * 60,
        now - int(0.4 * 86400): 90,
    }

    db.init(cfg.db_path)
    with db.connect(cfg.db_path) as conn:
        conn.execute("BEGIN")
        latency_rows, http_rows = [], []
        for step in range(days * 86400 // interval):
            ts = now - (days * 86400) + step * interval
            in_outage = any(start <= ts < start + length for start, length in outages.items())
            hour = time.localtime(ts).tm_hour
            # evening congestion adds a little latency
            congestion = 1.35 if 19 <= hour < 23 else 1.0

            for name in icmp:
                base = BASE_RTT.get(name, 12.0)
                lan = name == "Router"
                dead = in_outage and not lan
                jitter = abs(rng.gauss(0, base * 0.06))
                spike = base * 4 if rng.random() < 0.004 else 0
                rtt = None if dead else base * (1 if lan else congestion) + jitter + spike
                loss = 100.0 if dead else (20.0 if rng.random() < 0.002 else 0.0)
                latency_rows.append({
                    "ts": ts, "target": name, "host": "seed", "family": "ipv4",
                    "sent": 5, "recv": 0 if dead else 5, "loss_pct": loss,
                    "rtt_min": None if dead else round(rtt * 0.94, 3),
                    "rtt_avg": None if dead else round(rtt, 3),
                    "rtt_max": None if dead else round(rtt * 1.3, 3),
                    "rtt_mdev": None if dead else round(jitter, 3),
                    "error": "seeded outage" if dead else None,
                })

            if step % 5 == 0:
                for target in http_targets:
                    ttfb = None if in_outage else 90 + abs(rng.gauss(0, 25)) * congestion
                    http_rows.append({
                        "ts": ts, "target": target["name"], "url": target["url"],
                        "ok": 0 if in_outage else 1,
                        "status": None if in_outage else 200,
                        "dns_ms": None if in_outage else round(abs(rng.gauss(9, 3)), 2),
                        "connect_ms": None if in_outage else round(abs(rng.gauss(11, 3)), 2),
                        "tls_ms": None if in_outage else round(abs(rng.gauss(38, 8)), 2),
                        "ttfb_ms": None if ttfb is None else round(ttfb, 2),
                        "total_ms": None if ttfb is None else round(ttfb * 1.4, 2),
                        "error": "seeded outage" if in_outage else None,
                    })

            if len(latency_rows) > 20000:
                db.insert_latency(conn, latency_rows)
                latency_rows = []

        db.insert_latency(conn, latency_rows)
        db.insert_http(conn, http_rows)

        # throughput: alternating engines every 30 minutes, evening dip
        engines = cfg.get("speed.engines", ["ookla"])
        advertised = float(cfg.get("site.isp.advertised_down_mbps", 500)) or 500
        for i in range(days * 48):
            ts = now - (days * 86400) + i * 1800
            if any(start <= ts < start + length for start, length in outages.items()):
                continue
            hour = time.localtime(ts).tm_hour
            evening = 0.78 if 19 <= hour < 23 else 1.0
            wobble = math.sin(i / 9.0) * 0.04
            down = advertised * (0.92 + wobble) * evening + rng.gauss(0, advertised * 0.02)
            if rng.random() < 0.01:
                down *= 0.4  # occasional bad result
            engine = engines[i % len(engines)]
            profile = ENGINE_PROFILES.get(engine, ENGINE_PROFILES["cloudflare"])
            db.insert_speed(conn, {
                "ts": ts, "engine": engine, "ok": 1,
                # speedtest-cli reads low on a Pi because the python client is
                # the bottleneck — the demo data shows that, because real data will.
                "down_mbps": round(max(5, down * profile["bias"]), 2),
                "up_mbps": round(max(2, advertised * 0.14 * (0.95 + wobble)), 2),
                "ping_ms": round(8.5 * (1.3 if evening < 1 else 1) + abs(rng.gauss(0, 1)), 2),
                "jitter_ms": (round(abs(rng.gauss(0.6, 0.3)), 2)
                              if profile["jitter"] else None),
                "loss_pct": 0.0 if profile["jitter"] else None,
                "server": profile["server"], "server_id": profile["server_id"],
                "isp": "Demo Fibre", "external_ip": "203.0.113.10",
                "result_url": profile["url"],
                "bytes_down": profile["bytes_down"], "bytes_up": profile["bytes_up"],
                "duration_s": profile["duration"], "error": None, "raw": None,
            })
        conn.execute("COMMIT")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="config/config.example.yaml")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--db", default="/tmp/broadband-demo.db")
    parser.add_argument("--out", default="site", help="where to write data/")
    parser.add_argument("--serve", action="store_true", help="serve --out on :8000 afterwards")
    args = parser.parse_args()

    cfg = load_config(args.config)
    cfg.data["database"]["path"] = args.db
    Path(args.db).unlink(missing_ok=True)

    print(f"seeding {args.days} days into {args.db} …")
    seed(cfg, args.days)
    result = build_export(cfg, Path(args.out))
    print(f"exported {len(result['days'])} day files ({result['bytes'] / 1024:.0f} kB) "
          f"into {args.out}/data")

    if args.serve:
        import http.server
        import os

        os.chdir(args.out)
        print("serving http://localhost:8000/  (ctrl-c to stop)")
        http.server.test(
            HandlerClass=http.server.SimpleHTTPRequestHandler, port=8000, bind="127.0.0.1"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
