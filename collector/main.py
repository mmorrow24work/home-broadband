"""Command line entrypoint.

Each subcommand is a single short-lived run driven by a systemd timer, so the
process is never long-lived and a crash can never wedge collection.

    python3 -m collector.main latency     # one probe sweep of all targets
    python3 -m collector.main speed       # one throughput test (engine alternates)
    python3 -m collector.main publish     # aggregate + push to GitHub Pages
    python3 -m collector.main status      # human-readable summary
    python3 -m collector.main servers     # find a speedtest server worth pinning
    python3 -m collector.main init-db     # create the schema
    python3 -m collector.main prune       # apply the retention policy
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import logging
import subprocess
import sys
import time
from typing import Any

from . import __version__, db, link
from .config import VALID_ENGINES, Config, ConfigError, load_config
from .probes import cloudflare, ookla, ping, speedtest_cli
from .probes import dns as dns_probe
from .probes import http as http_probe

log = logging.getLogger("broadband")

# Engine id -> (config block, runner). The id is what lands in the database and
# on the dashboard, so it stays stable even if the module is renamed.
SPEED_ENGINES = {
    "ookla": ("speed.ookla", ookla.run),
    "speedtest-cli": ("speed.speedtest-cli", speedtest_cli.run),
    "cloudflare": ("speed.cloudflare", cloudflare.run),
}


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


# ---------------------------------------------------------------------------
# latency sweep
# ---------------------------------------------------------------------------
def cmd_latency(cfg: Config, args: argparse.Namespace) -> int:
    settings = cfg.get("latency", {})
    workers = max(1, int(settings.get("parallel", 8)))

    icmp_targets = cfg.targets_with("icmp")
    dns_targets = cfg.targets_with("dns")
    http_targets = cfg.targets_with("http")

    latency_rows: list[dict[str, Any]] = []
    dns_rows: list[dict[str, Any]] = []
    http_rows: list[dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures: dict[concurrent.futures.Future, tuple[str, str]] = {}
        for target in icmp_targets:
            futures[pool.submit(ping.probe, target, settings)] = ("icmp", target["name"])
        for target in dns_targets:
            futures[pool.submit(dns_probe.probe, target, settings.get("dns", {}))] = (
                "dns",
                target["name"],
            )
        for target in http_targets:
            futures[pool.submit(http_probe.probe, target, settings.get("http", {}))] = (
                "http",
                target["name"],
            )

        for future in concurrent.futures.as_completed(futures):
            kind, name = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                log.error("%s probe for %s raised: %s", kind, name, exc)
                continue
            if kind == "icmp":
                latency_rows.append(result)
            elif kind == "dns":
                dns_rows.extend(result)
            else:
                http_rows.append(result)

    if args.dry_run:
        for row in latency_rows:
            log.info(
                "ICMP %-16s loss=%5.1f%% avg=%s ms%s",
                row["target"],
                row["loss_pct"],
                row["rtt_avg"],
                f"  [{row['error']}]" if row["error"] else "",
            )
        for row in dns_rows:
            log.info("DNS  %-16s via %-8s %s ms %s", row["target"], row["resolver"],
                     row["ms"], row["error"] or "")
        for row in http_rows:
            log.info("HTTP %-16s status=%s ttfb=%s ms %s", row["target"], row["status"],
                     row["ttfb_ms"], row["error"] or "")
        return 0

    with db.connect(cfg.db_path) as conn:
        if latency_rows:
            db.insert_latency(conn, latency_rows)
        if dns_rows:
            db.insert_dns(conn, dns_rows)
        if http_rows:
            db.insert_http(conn, http_rows)

    down = [row["target"] for row in latency_rows if row["loss_pct"] >= 100]
    log.info(
        "latency sweep: %d icmp, %d dns, %d http%s",
        len(latency_rows),
        len(dns_rows),
        len(http_rows),
        f" — UNREACHABLE: {', '.join(down)}" if down else "",
    )
    return 0


# ---------------------------------------------------------------------------
# throughput test
# ---------------------------------------------------------------------------
def _in_quiet_hours(cfg: Config, now: dt.datetime) -> bool:
    for window in cfg.get("speed.quiet_hours", []) or []:
        start = dt.time.fromisoformat(str(window["start"]))
        end = dt.time.fromisoformat(str(window["end"]))
        current = now.time()
        inside = start <= current < end if start <= end else (current >= start or current < end)
        if inside:
            return True
    return False


def cmd_speed(cfg: Config, args: argparse.Namespace) -> int:
    engines = cfg.get("speed.engines", ["ookla"])
    now = dt.datetime.now()

    if _in_quiet_hours(cfg, now) and not args.force:
        log.info("inside speed.quiet_hours — skipping throughput test")
        return 0

    db.init(cfg.db_path)
    with db.connect(cfg.db_path) as conn:
        engine = args.engine or db.next_engine(conn, engines)

        max_daily_gb = float(cfg.get("speed.max_daily_gb", 0) or 0)
        if max_daily_gb and not args.force:
            midnight = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            used = db.bytes_used_since(conn, midnight)
            if used >= max_daily_gb * 1_000_000_000:
                log.warning(
                    "daily data guard hit (%.2f GB used, limit %.2f GB) — skipping",
                    used / 1e9,
                    max_daily_gb,
                )
                return 0

        config_key, runner = SPEED_ENGINES[engine]
        log.info("running %s throughput test", engine)
        row = runner(cfg.get(config_key, {}))

        if args.dry_run:
            log.info("%s", row)
            return 0

        db.insert_speed(conn, row)

    if row["ok"]:
        caveat = link.warn_if_nic_bound(row["down_mbps"], row["up_mbps"])
        if caveat:
            log.warning("%s", caveat)
        log.info(
            "%s: down %.1f Mbps / up %.1f Mbps / ping %s ms via %s (%.2f GB used)",
            row["engine"],
            row["down_mbps"] or 0,
            row["up_mbps"] or 0,
            row["ping_ms"],
            row["server"],
            (row["bytes_down"] + row["bytes_up"]) / 1e9,
        )
        return 0

    log.error("%s test failed: %s", row["engine"], row["error"])
    return 1


# ---------------------------------------------------------------------------
# publish / maintenance / status
# ---------------------------------------------------------------------------
def cmd_publish(cfg: Config, args: argparse.Namespace) -> int:
    from .publish import publish  # imported lazily; keeps `speed` runs light

    return publish(cfg, dry_run=args.dry_run, export_only=args.export_only)


def cmd_servers(cfg: Config, args: argparse.Namespace) -> int:
    """List candidate speedtest.net servers to pin in config.yaml.

    Automatic server selection is driven by speedtest.net's geolocation of your
    IP, which is often wrong on carrier ranges — a UK line can be offered
    servers in another country, which then reads as a slow connection.
    """
    try:
        servers = speedtest_cli.list_servers(cfg.get("speed.speedtest-cli", {}))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log.error("could not list servers: %s", exc)
        return 1

    if not servers:
        log.error("no servers parsed from `speedtest-cli --list`")
        return 1

    matching = servers
    if args.country:
        needle = args.country.lower()
        matching = [s for s in servers if needle in s["country"].lower()]
    if args.grep:
        needle = args.grep.lower()
        matching = [
            s for s in matching
            if needle in s["sponsor"].lower() or needle in s["city"].lower()
        ]

    nearest = servers[0]
    print(
        f"speedtest.net thinks your nearest server is {nearest['km']:.0f} km away "
        f"({nearest['city']}, {nearest['country']})."
    )
    if nearest["km"] > 200:
        print(
            "  That is a long way for a home line. Your IP is probably geolocated\n"
            "  badly, which is exactly why automatic selection picks poor servers.\n"
            "  Pin one near you instead."
        )
    print()

    if not matching:
        print(f"No servers matched. Try --country '' to see all {len(servers)}.")
        return 1

    print(f"{'ID':>8}  {'DISTANCE':>9}  SPONSOR / LOCATION")
    for server in matching[: args.limit]:
        print(
            f"{server['id']:>8}  {server['km']:>7.0f} km  "
            f"{server['sponsor']} — {server['city']}, {server['country']}"
        )
    if len(matching) > args.limit:
        print(f"... and {len(matching) - args.limit} more (raise --limit)")

    print("\nPin one in /etc/broadband-monitor/config.yaml:")
    print("  speed:")
    print("    speedtest-cli:")
    print(f"      server_id: {matching[0]['id']}")
    print("    ookla:")
    print(f"      server_id: {matching[0]['id']}    # same id, directly comparable")
    return 0


def cmd_init_db(cfg: Config, _args: argparse.Namespace) -> int:
    db.init(cfg.db_path)
    log.info("initialised %s", cfg.db_path)
    return 0


def cmd_prune(cfg: Config, _args: argparse.Namespace) -> int:
    with db.connect(cfg.db_path) as conn:
        deleted = db.prune(conn, int(cfg.get("database.retention_days", 0)))
        conn.execute("VACUUM")
    log.info("pruned: %s", deleted or "nothing (retention disabled)")
    return 0


def cmd_status(cfg: Config, _args: argparse.Namespace) -> int:
    with db.connect(cfg.db_path, readonly=True) as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("latency", "dns", "http", "speed")
        }
        last_speed = conn.execute(
            "SELECT * FROM speed WHERE ok = 1 ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        recent = conn.execute(
            """SELECT target, ROUND(AVG(rtt_avg), 2) AS avg_rtt,
                      ROUND(AVG(loss_pct), 2) AS avg_loss, COUNT(*) AS n
               FROM latency WHERE ts >= ? GROUP BY target ORDER BY target""",
            (int(time.time()) - 86400,),
        ).fetchall()

    nic = link.describe()
    print(f"home-broadband {__version__}   config: {cfg.source}")
    if nic["interface"]:
        speed = nic["link_speed_mbps"]
        print(
            f"link:     {nic['interface']} @ "
            + (f"{speed} Mbit" if speed else "unknown speed (wireless?)")
        )
    print(f"database: {cfg.db_path}")
    print("  rows: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if last_speed:
        age = (time.time() - last_speed["ts"]) / 60
        print(
            f"  last speed test ({last_speed['engine']}, {age:.0f} min ago): "
            f"{last_speed['down_mbps']} / {last_speed['up_mbps']} Mbps, "
            f"{last_speed['ping_ms']} ms"
        )
    print("\nlast 24h latency:")
    for row in recent:
        print(
            f"  {row['target']:<18} avg {row['avg_rtt'] or '-':>7} ms   "
            f"loss {row['avg_loss'] or 0:>5}%   n={row['n']}"
        )
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="broadband-monitor",
        description="Continuous home broadband quality collector.",
    )
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_latency = sub.add_parser("latency", help="probe every configured target once")
    p_latency.add_argument("--dry-run", action="store_true", help="print, do not store")
    p_latency.set_defaults(func=cmd_latency)

    p_speed = sub.add_parser("speed", help="run one throughput test")
    p_speed.add_argument(
        "--engine", choices=sorted(VALID_ENGINES), help="run this engine instead of alternating"
    )
    p_speed.add_argument("--force", action="store_true", help="ignore quiet hours and data guard")
    p_speed.add_argument("--dry-run", action="store_true", help="print, do not store")
    p_speed.set_defaults(func=cmd_speed)

    p_publish = sub.add_parser("publish", help="export JSON and push to GitHub Pages")
    p_publish.add_argument("--dry-run", action="store_true", help="build files, skip git push")
    p_publish.add_argument(
        "--export-only", action="store_true", help="write JSON into site/data and stop"
    )
    p_publish.set_defaults(func=cmd_publish)

    p_servers = sub.add_parser(
        "servers", help="list speedtest.net servers you could pin, filtered by country"
    )
    p_servers.add_argument(
        "--country", default="United Kingdom",
        help="substring match on country (default: %(default)s; pass '' for all)",
    )
    p_servers.add_argument("--grep", help="also filter by sponsor or city")
    p_servers.add_argument("--limit", type=int, default=20)
    p_servers.set_defaults(func=cmd_servers)

    sub.add_parser("init-db", help="create the database schema").set_defaults(func=cmd_init_db)
    sub.add_parser("prune", help="apply the retention policy").set_defaults(func=cmd_prune)
    sub.add_parser("status", help="print a summary of collected data").set_defaults(
        func=cmd_status
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2

    db.init(cfg.db_path)
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
