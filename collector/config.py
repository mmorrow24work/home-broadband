"""Configuration loading, defaults and validation."""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATHS = [
    Path("/etc/broadband-monitor/config.yaml"),
    Path(__file__).resolve().parent.parent / "config" / "config.yaml",
    Path(__file__).resolve().parent.parent / "config" / "config.example.yaml",
]

DEFAULTS: dict[str, Any] = {
    "site": {
        "title": "Home Broadband Monitor",
        "subtitle": "",
        "domain": "",
        "timezone": "Europe/London",
        "isp": {
            "name": "",
            "package": "",
            "advertised_down_mbps": 0,
            "advertised_up_mbps": 0,
            "guaranteed_min_down_mbps": 0,
            "target_max_ping_ms": 25,
        },
    },
    "database": {
        "path": "/var/lib/broadband-monitor/broadband.db",
        "retention_days": 1095,
    },
    "latency": {
        "interval_seconds": 60,
        "count": 5,
        "ping_interval": 0.25,
        "timeout_seconds": 6,
        "parallel": 8,
        "dns": {"resolvers": ["1.1.1.1"], "record": "A", "timeout_seconds": 3},
        "http": {"timeout_seconds": 10, "user_agent": "home-broadband-monitor/1.0"},
        "targets": [],
    },
    "speed": {
        "interval_minutes": 60,
        "engines": ["ookla", "cloudflare"],
        "max_daily_gb": 20,
        "quiet_hours": [],
        "ookla": {"binary": "speedtest", "server_id": None, "extra_args": []},
        "speedtest-cli": {
            "binary": "speedtest-cli",
            "server_id": None,
            "secure": True,
            "no_pre_allocate": True,
            "single": False,
            "share": False,
            "timeout_seconds": 30,
            "extra_args": [],
        },
        "cloudflare": {
            "streams": 4,
            "download_bytes": 250_000_000,
            "upload_bytes": 100_000_000,
            "duration_seconds": 12,
        },
    },
    "publish": {
        "enabled": True,
        "interval_minutes": 60,
        "remote": "",
        "branch": "gh-pages",
        "repo_dir": "/opt/broadband-monitor",
        "work_dir": "/var/lib/broadband-monitor/pages",
        "squash": True,
        "sync_code": True,
        "bucket_seconds": 300,
        "latest_hours": 48,
        "keep_daily_days": 400,
        "git_user_name": "broadband-monitor",
        "git_user_email": "broadband-monitor@users.noreply.github.com",
    },
}

# Hostnames, IPv4 and IPv6 literals (with optional %zone) and nothing else.
HOST_RE = re.compile(r"^[A-Za-z0-9._:%-]+$")

VALID_CHECKS = {"icmp", "dns", "http"}
VALID_FAMILIES = {"auto", "ipv4", "ipv6"}
VALID_ENGINES = {"ookla", "speedtest-cli", "cloudflare"}


class ConfigError(ValueError):
    """Raised when the configuration file is unusable."""


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Dotted-path accessor over the merged configuration dict."""

    def __init__(self, data: dict[str, Any], source: Path | None = None):
        self.data = data
        self.source = source

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # -- convenience ------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return Path(self.get("database.path"))

    @property
    def targets(self) -> list[dict[str, Any]]:
        return self.get("latency.targets", [])

    def targets_with(self, check: str) -> list[dict[str, Any]]:
        return [t for t in self.targets if check in t.get("checks", ["icmp"])]

    @property
    def primary_target(self) -> dict[str, Any] | None:
        for target in self.targets:
            if target.get("primary"):
                return target
        return self.targets[0] if self.targets else None


def find_config_file(explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    env = os.environ.get("BROADBAND_CONFIG")
    if env:
        return find_config_file(env)
    for candidate in DEFAULT_PATHS:
        if candidate.is_file():
            return candidate
    raise ConfigError(
        "no config file found; looked in " + ", ".join(str(p) for p in DEFAULT_PATHS)
    )


def load_config(explicit: str | os.PathLike | None = None) -> Config:
    path = find_config_file(explicit)
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    cfg = Config(_deep_merge(DEFAULTS, raw), path)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    targets = cfg.targets
    if not targets:
        raise ConfigError("latency.targets is empty — nothing to monitor")

    seen: set[str] = set()
    primaries = 0
    for index, target in enumerate(targets):
        where = f"latency.targets[{index}]"
        name = target.get("name")
        if not name:
            raise ConfigError(f"{where}: 'name' is required")
        if name in seen:
            raise ConfigError(f"{where}: duplicate target name {name!r}")
        seen.add(name)

        host = target.get("host")
        if not host:
            raise ConfigError(f"{where} ({name}): 'host' is required")
        # A hostname or IP literal only ever contains these. Anything else is a
        # paste accident — a markdown link, a full URL, or a stray quote — and
        # would otherwise fail silently as an unresolvable target forever.
        if not HOST_RE.match(str(host)):
            raise ConfigError(
                f"{where} ({name}): {host!r} is not a hostname or IP address. "
                "Use the bare host (www.bbc.co.uk), not a URL or a markdown link."
            )

        checks = target.get("checks") or ["icmp"]
        if not isinstance(checks, list):
            raise ConfigError(f"{where} ({name}): 'checks' must be a list")
        unknown = set(checks) - VALID_CHECKS
        if unknown:
            raise ConfigError(
                f"{where} ({name}): unknown checks {sorted(unknown)}; "
                f"valid values are {sorted(VALID_CHECKS)}"
            )
        if "http" in checks and not target.get("url"):
            raise ConfigError(f"{where} ({name}): 'url' is required when using the http check")

        if target.get("expect_status") is not None:
            from .probes.http import parse_expect

            try:
                parse_expect(target["expect_status"])
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"{where} ({name}): expect_status must be a status code, a "
                    f"'NNN-NNN' range, or a list of those — {exc}"
                ) from exc

        family = target.get("family", "auto")
        if family not in VALID_FAMILIES:
            raise ConfigError(
                f"{where} ({name}): family must be one of {sorted(VALID_FAMILIES)}"
            )
        if target.get("primary"):
            primaries += 1

    if primaries > 1:
        raise ConfigError("only one target may set primary: true")

    engines = cfg.get("speed.engines", [])
    if not engines:
        raise ConfigError("speed.engines is empty — no throughput test would ever run")
    unknown_engines = set(engines) - VALID_ENGINES
    if unknown_engines:
        raise ConfigError(
            f"speed.engines: unknown engine(s) {sorted(unknown_engines)}; "
            f"valid values are {sorted(VALID_ENGINES)}"
        )
    if len(engines) != len(set(engines)):
        raise ConfigError("speed.engines: each engine may only be listed once")

    if cfg.get("publish.enabled") and not cfg.get("publish.remote"):
        raise ConfigError("publish.enabled is true but publish.remote is empty")

    for index, window in enumerate(cfg.get("speed.quiet_hours", []) or []):
        if not isinstance(window, dict) or "start" not in window or "end" not in window:
            raise ConfigError(
                f"speed.quiet_hours[{index}]: expected a mapping with 'start' and 'end' (HH:MM)"
            )
