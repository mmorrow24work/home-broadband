"""On-demand tests requested from the dashboard.

The collector never listens on the network, so the dashboard cannot reach it.
Instead a request travels through GitHub, which already authenticates people:

    dashboard ──workflow_dispatch──► .github/workflows/run-test.yml
                                         │ writes requests/<collector>.json
                                         ▼
                                   `control` branch
                                         ▲
    collector ──git fetch (every minute, deploy key, outbound only)──┘

Only someone allowed to run the repository's workflows can place a request,
and the collector reads nothing but a test name, an engine and an id from it,
each checked against a fixed list — the request file can never carry a command.

Each request has an id. The collector remembers which ids it has handled, so a
request runs once however many times it is fetched, and it ignores requests
older than `control.max_age_minutes` so a collector that was off for a day does
not wake up and run a stale one. What happened is published in the manifest
(`on_demand`), which is how the dashboard knows the result is in.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from . import publish as pub
from .config import VALID_ENGINES, Config

log = logging.getLogger("broadband.control")

REQUEST_DIR = "requests"
ALL_COLLECTORS = "_all"
VALID_TESTS = ("speed", "latency", "both")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
WHO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")  # a GitHub login
HANDLED_KEPT = 50   # ids remembered, so a request already run never repeats
RECENT_KEPT = 5     # outcomes published for the dashboard
CLOCK_SKEW = 300    # seconds a request may claim to come from the future


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def state_path(cfg: Config) -> Path:
    return Path(cfg.get("publish.work_dir")) / "control-state.json"


def load_state(cfg: Config) -> dict[str, Any]:
    try:
        data = json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("handled", [])
    data.setdefault("recent", [])
    return data


def save_state(cfg: Config, state: dict[str, Any]) -> None:
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["handled"] = state["handled"][-HANDLED_KEPT:]
    state["recent"] = state["recent"][-RECENT_KEPT:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)


def published_status(cfg: Config) -> list[dict[str, Any]]:
    """The last few on-demand outcomes, newest first, for the manifest."""
    return list(reversed(load_state(cfg)["recent"]))


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------
def parse_request(raw: Any, engines: list[str]) -> dict[str, Any] | None:
    """Validate one request file. Anything unexpected is dropped, not guessed at."""
    if not isinstance(raw, dict):
        return None
    req_id = str(raw.get("id", ""))
    test = str(raw.get("test", ""))
    if not ID_RE.match(req_id) or test not in VALID_TESTS:
        return None
    try:
        requested_at = int(raw.get("requested_at"))
    except (TypeError, ValueError):
        return None

    engine = raw.get("engine") or None
    if engine in ("auto", ""):
        engine = None
    if engine is not None and (engine not in VALID_ENGINES or engine not in engines):
        log.warning("request %s names engine %r, which this collector does not run — "
                    "using the rotation instead", req_id, engine)
        engine = None

    who = str(raw.get("by") or "")
    return {
        "id": req_id,
        "test": test,
        "engine": engine,
        "requested_at": requested_at,
        "by": who if WHO_RE.match(who) else None,
    }


def fetch_requests(cfg: Config) -> list[dict[str, Any]]:
    """Fetch the control branch and return the raw requests aimed at us.

    Uses a scratch repo next to the publisher's, the same remote and the same
    deploy key, so a collector that can publish can also be controlled.
    """
    pub.GIT = pub.find_git(cfg.get("publish.git_binary") or "")
    branch = cfg.get("control.branch", "control")
    repo = Path(cfg.get("publish.work_dir")) / "control"
    env = pub.git_env(cfg)

    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        pub._git(["init", "-q"], repo)
        pub._git(["remote", "add", "origin", cfg.get("publish.remote")], repo)
    else:
        pub._git(["remote", "set-url", "origin", cfg.get("publish.remote")], repo)

    base = pub._fetch_published(repo, branch, env)
    if base is None:
        return []  # nobody has asked for anything yet

    found = []
    for name in (pub.collector_id(cfg), ALL_COLLECTORS):
        shown = pub._git(["show", f"{base}:{REQUEST_DIR}/{name}.json"], repo, check=False)
        if shown.returncode != 0:
            continue
        try:
            found.append(json.loads(shown.stdout))
        except ValueError:
            log.warning("ignoring unreadable %s/%s.json on %s", REQUEST_DIR, name, branch)
    return found


def select(
    raw_requests: list[Any], state: dict[str, Any], engines: list[str],
    max_age: int, now: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Pick the request to run, plus the others to mark handled without running.

    Several can be pending at once (one for this collector, one for all of
    them); running each back to back would only repeat the measurement, so the
    newest runs and the rest are recorded as covered by it.
    """
    handled = set(state["handled"])
    fresh, skipped = [], []
    for raw in raw_requests:
        req = parse_request(raw, engines)
        if req is None or req["id"] in handled:
            continue
        age = now - req["requested_at"]
        if age > max_age or age < -CLOCK_SKEW:
            skipped.append({**req, "outcome": "expired"})
        else:
            fresh.append(req)
    fresh.sort(key=lambda r: r["requested_at"])
    if not fresh:
        return None, skipped
    chosen = fresh[-1]
    skipped += [{**r, "outcome": "superseded", "by_request": chosen["id"]} for r in fresh[:-1]]
    return chosen, skipped


def record(state: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """Remember a request as handled and add it to the published outcomes."""
    state["handled"].append(req["id"])
    entry = {k: v for k, v in req.items() if v is not None}
    state["recent"].append(entry)
    return entry
