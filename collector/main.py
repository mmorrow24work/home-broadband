"""Command line entrypoint.

Each subcommand is a single short-lived run driven by a systemd timer, so the
process is never long-lived and a crash can never wedge collection.

    python3 -m collector.main latency     # one probe sweep of all targets
    python3 -m collector.main speed       # one throughput test (engine alternates)
    python3 -m collector.main lan         # iperf3 against a LAN box (Wi-Fi alone)
    python3 -m collector.main publish     # aggregate + push to GitHub Pages
    python3 -m collector.main control     # run a test requested from the dashboard
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
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, control, db, link, lock
from .config import VALID_ENGINES, Config, ConfigError, load_config
from .paths import CONFIG_FILE
from .probes import cloudflare, iperf, ookla, ping, speedtest_cli
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


def setup_logging(verbose: bool = False, log_file: str | None = None) -> None:
    """Log to stderr (systemd's journal on Linux), plus a file if asked.

    Windows Task Scheduler throws a task's output away, so the scheduled tasks
    pass --log-file. One file per subcommand keeps writers from colliding.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        from pathlib import Path

        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        handlers=handlers,
        force=True,  # apply even if something already configured the root logger
    )


# ---------------------------------------------------------------------------
# latency sweep
# ---------------------------------------------------------------------------
def _away(cfg: Config) -> str | None:
    """Reason to skip measuring: this machine is on someone else's network."""
    return link.away_from_home(list(cfg.get("network.home_gateways") or []))


def cmd_latency(cfg: Config, args: argparse.Namespace) -> int:
    away = _away(cfg)
    if away and not args.dry_run:
        log.info("not on the home network (%s) — skipping the sweep", away)
        return 0
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


def _lock_path(cfg: Config, name: str) -> Path:
    return Path(cfg.db_path).parent / f"{name}.lock"


def run_speed(
    cfg: Config,
    engine: str | None = None,
    *,
    force: bool = False,
    on_demand: bool = False,
    dry_run: bool = False,
) -> tuple[int, str]:
    """One throughput test. Returns (exit code, one-line outcome).

    An on-demand run was asked for by a person, so quiet hours do not apply,
    but the daily data guard still does: it protects a metered bill.
    """
    engines = cfg.get("speed.engines", ["ookla"])
    now = dt.datetime.now()

    away = _away(cfg)
    if away and not dry_run:
        # Not even --force: a 4G hotspot's numbers would sit in the line's history.
        log.info("not on the home network (%s) — skipping throughput test", away)
        return 0, "skipped: not on the home network"

    if _in_quiet_hours(cfg, now) and not (force or on_demand):
        log.info("inside speed.quiet_hours — skipping throughput test")
        return 0, "skipped: quiet hours"

    db.init(cfg.db_path)
    try:
        # A scheduled test that finds another running gives way at once: the
        # line is being measured already. An on-demand one waits its turn.
        with lock.held(_lock_path(cfg, "speed"), wait=240 if on_demand else 0):
            return _speed_locked(cfg, engines, engine, now, force, dry_run)
    except lock.Busy:
        log.info("another throughput test is running — skipping this one")
        return 0, "skipped: another throughput test was running"


def _speed_locked(
    cfg: Config, engines: list[str], engine: str | None, now: dt.datetime,
    force: bool, dry_run: bool,
) -> tuple[int, str]:
    with db.connect(cfg.db_path) as conn:
        engine = engine or db.next_engine(conn, engines)

        max_daily_gb = float(cfg.get("speed.max_daily_gb", 0) or 0)
        if max_daily_gb and not force:
            midnight = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            used = db.bytes_used_since(conn, midnight)
            if used >= max_daily_gb * 1_000_000_000:
                log.warning(
                    "daily data guard hit (%.2f GB used, limit %.2f GB) — skipping",
                    used / 1e9,
                    max_daily_gb,
                )
                return 0, f"skipped: daily data limit of {max_daily_gb:g} GB reached"

        config_key, runner = SPEED_ENGINES[engine]
        log.info("running %s throughput test", engine)
        row = runner(cfg.get(config_key, {}))

        if dry_run:
            log.info("%s", row)
            return 0, "dry run"

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
        return 0, (
            f"{row['engine']}: {row['down_mbps'] or 0:.1f} down / "
            f"{row['up_mbps'] or 0:.1f} up Mbps"
        )

    log.error("%s test failed: %s", row["engine"], row["error"])
    return 1, f"{row['engine']} failed: {str(row['error'])[:160]}"


def cmd_lan(cfg: Config, args: argparse.Namespace) -> int:
    """One local-network throughput test (Wi-Fi alone, when this box is on Wi-Fi)."""
    settings = cfg.get("lan", {})
    if not settings.get("enabled") or not settings.get("server"):
        log.debug("lan test disabled or lan.server empty — nothing to do")
        return 0
    away = _away(cfg)
    if away:
        log.info("not on the home network (%s) — skipping the LAN test", away)
        return 0
    try:
        # It saturates the same Wi-Fi an internet test would; never both at once.
        with lock.held(_lock_path(cfg, "speed"), wait=0):
            row = iperf.run(settings)
    except lock.Busy:
        log.info("a throughput test is running — skipping the LAN test")
        return 0

    radio = ""
    if row.get("channel"):
        radio = (f" over {row['band']} ch {row['channel']}, {row['rssi_dbm']} dBm, "
                 f"link {row['rx_rate_mbps']}/{row['tx_rate_mbps']} Mbps")
    if args.dry_run:
        log.info("%s", row)
        return 0
    with db.connect(cfg.db_path) as conn:
        db.insert_lan(conn, row)
    if not row["ok"]:
        log.error("LAN test to %s failed: %s", row["server"], row["error"])
        return 1
    log.info("LAN test to %s: down %.1f / up %.1f Mbps%s",
             row["server"], row["down_mbps"], row["up_mbps"], radio)
    return 0


def cmd_speed(cfg: Config, args: argparse.Namespace) -> int:
    code, _ = run_speed(cfg, args.engine, force=args.force, dry_run=args.dry_run)
    return code


# ---------------------------------------------------------------------------
# on-demand tests
# ---------------------------------------------------------------------------
def cmd_control(cfg: Config, args: argparse.Namespace) -> int:
    """Run a test someone asked for from the dashboard, then publish it."""
    if not cfg.get("control.enabled") or not cfg.get("publish.remote"):
        log.debug("control disabled or no publish.remote — nothing to check")
        return 0

    try:
        raw = control.fetch_requests(cfg)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        log.warning("could not check for requested tests: %s", exc)
        return 1

    state = control.load_state(cfg)
    max_age = int(cfg.get("control.max_age_minutes", 15)) * 60
    chosen, skipped = control.select(
        raw, state, cfg.get("speed.engines", []), max_age, int(time.time())
    )
    for req in skipped:
        log.info("request %s %s", req["id"], req["outcome"])
        control.record(state, req)
    if chosen is None:
        if skipped:
            control.save_state(cfg, state)
        return 0

    log.info(
        "running requested %s test %s%s", chosen["test"], chosen["id"],
        f" (asked by {chosen['by']})" if chosen["by"] else "",
    )
    # Recorded before running, so a crash mid-test can never loop on it.
    entry = control.record(state, {**chosen, "outcome": "running",
                                   "started_at": int(time.time())})
    control.save_state(cfg, state)

    results, code = [], 0
    away = None if args.dry_run else _away(cfg)
    if away:
        log.info("not on the home network (%s) — not running request %s", away, chosen["id"])
        entry.update(outcome="skipped", finished_at=int(time.time()),
                     detail="skipped: not on the home network")
        control.save_state(cfg, state)
        if args.no_publish:
            return 0
        return cmd_publish(cfg, argparse.Namespace(dry_run=False, export_only=False))

    if chosen["test"] in ("latency", "both"):
        rc = cmd_latency(cfg, argparse.Namespace(dry_run=args.dry_run))
        results.append("latency sweep stored" if rc == 0 else "latency sweep failed")
        code = code or rc
    if chosen["test"] in ("speed", "both"):
        rc, message = run_speed(cfg, chosen["engine"], on_demand=True, dry_run=args.dry_run)
        results.append(message)
        code = code or rc

    entry.update(
        outcome="ok" if code == 0 else "failed",
        finished_at=int(time.time()),
        detail="; ".join(results),
    )
    control.save_state(cfg, state)

    if args.dry_run or args.no_publish:
        return code
    return cmd_publish(cfg, argparse.Namespace(dry_run=False, export_only=False)) or code


# ---------------------------------------------------------------------------
# publish / maintenance / status
# ---------------------------------------------------------------------------
def cmd_publish(cfg: Config, args: argparse.Namespace) -> int:
    from .publish import publish  # imported lazily; keeps `speed` runs light

    # The timer and an on-demand run can both publish; they share a scratch repo.
    try:
        with lock.held(_lock_path(cfg, "publish"), wait=300):
            return publish(cfg, dry_run=args.dry_run, export_only=args.export_only)
    except lock.Busy:
        log.warning("another publish has been running for 5 minutes — skipping this one")
        return 1


def cmd_setup_check(cfg: Config, args: argparse.Namespace) -> int:
    from .setup_check import pending

    for step, detail in pending(cfg):
        print(f"{step}\t{detail}")
    return 0


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

    print(f"\nPin one in {cfg.source or CONFIG_FILE}:")
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
            for table in ("latency", "dns", "http", "speed", "lan")
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
    parser.add_argument("--log-file", help="also append logs to this file (rotated at 2 MB)")
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

    p_lan = sub.add_parser("lan", help="one iperf3 test against lan.server (Wi-Fi alone)")
    p_lan.add_argument("--dry-run", action="store_true", help="print, do not store")
    p_lan.set_defaults(func=cmd_lan)

    p_control = sub.add_parser(
        "control", help="run a test requested from the dashboard, if there is one"
    )
    p_control.add_argument("--dry-run", action="store_true", help="print, do not store")
    p_control.add_argument(
        "--no-publish", action="store_true", help="store the result; leave publishing to the timer"
    )
    p_control.set_defaults(func=cmd_control)

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
    sub.add_parser(
        "setup-check", help="list the setup steps still outstanding (used by the installers)"
    ).set_defaults(func=cmd_setup_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.log_file)
    log.debug("%s starting (pid %d)", args.command, os.getpid())
    try:
        try:
            cfg = load_config(args.config)
        except ConfigError as exc:
            log.error("configuration error: %s", exc)
            return 2

        db.init(cfg.db_path)
        return args.func(cfg, args)
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Task Scheduler discards stderr, so an uncaught traceback would leave
        # nothing but exit code 1 and an empty log. Put it in the log instead.
        log.exception("%s crashed", args.command)
        return 1


if __name__ == "__main__":
    sys.exit(main())
