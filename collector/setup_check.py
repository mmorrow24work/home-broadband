"""What is still left to do on this collector, for the installers' summary.

The installers used to print every first-run step on every run, long after
they were done. They now run `collector.main setup-check` and show only what
this prints. Each line is "<step>\t<detail>":

  config   a setting that still needs a value (one line per setting)
  key      nothing has been pushed yet: add the deploy key with write access
  smoke    nothing has been pushed yet: run the first publish by hand
  pages    pushed, but the live site does not list this collector yet
  ok       nothing left to do; detail is the site URL

Everything here checks real state (the actual gateway, the publish repo, the
live site) because the example config ships with someone's real values, so
"still equal to the example" proves nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import link
from . import publish as pub
from .config import Config
from .paths import find_git

EXAMPLE_ISP = (500, 70, 250)  # the speeds config.example.yaml ships with


def site_url(cfg: Config) -> str | None:
    domain = (cfg.get("site.domain") or "").strip()
    if domain:
        return f"https://{domain}/"
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", cfg.get("publish.remote") or "")
    return f"https://{m.group(1).lower()}.github.io/{m.group(2)}/" if m else None


def has_published(cfg: Config) -> bool:
    """A push has succeeded: the branch built locally matches what origin took."""
    repo = Path(cfg.get("publish.work_dir")) / "repo"
    if not (repo / ".git").exists():
        return False
    pub.GIT = find_git(cfg.get("publish.git_binary") or "")
    branch = cfg.get("publish.branch", "gh-pages")
    try:
        local = pub._git(["rev-parse", "-q", "--verify", f"refs/heads/{branch}"], repo, check=False)
        remote = pub._git(
            ["rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}"], repo, check=False
        )
    except OSError:
        return False
    return bool(local.stdout.strip()) and local.stdout.strip() == remote.stdout.strip()


def site_lists_collector(cfg: Config, url: str) -> bool | None:
    """True if the live collectors.json names this collector; None if unreachable."""
    import requests

    try:
        response = requests.get(f"{url}data/collectors.json", timeout=8)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return False
    try:
        ids = {c.get("id") for c in response.json().get("collectors", [])}
    except ValueError:
        return False
    return pub.collector_id(cfg) in ids


def config_problems(cfg: Config, gateway: str | None) -> list[str]:
    problems = []
    if cfg.get("publish.enabled") and not (cfg.get("publish.remote") or "").strip():
        problems.append("publish.remote is empty: set your repo's SSH URL")

    lan = [t for t in cfg.targets if t.get("group") == "lan"]
    if gateway and not any(t.get("host") == gateway for t in lan):
        where = f"is {lan[0]['host']}" if lan else "is missing"
        problems.append(f"the Router target {where}, but this machine's gateway is {gateway}")

    isp = (
        cfg.get("site.isp.advertised_down_mbps"),
        cfg.get("site.isp.advertised_up_mbps"),
        cfg.get("site.isp.guaranteed_min_down_mbps"),
    )
    if tuple(int(x or 0) for x in isp) == EXAMPLE_ISP:
        problems.append("site.isp still has the example speeds (500/70 Mbps, 250 minimum)")
    return problems


def pending(cfg: Config) -> list[tuple[str, str]]:
    todo = [("config", p) for p in config_problems(cfg, link.default_gateway())]
    if not cfg.get("publish.enabled"):
        todo.append(("config", "publish.enabled is false: nothing will reach the site"))
        return todo

    url = site_url(cfg) or ""
    if not has_published(cfg):
        todo += [("key", ""), ("smoke", "")]
    elif url and site_lists_collector(cfg, url) is False:
        todo.append(("pages", url))
    if not todo:
        todo.append(("ok", url))
    return todo
