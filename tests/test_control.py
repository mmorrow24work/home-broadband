"""On-demand tests: requests via the control branch, results in the manifest."""

import argparse
import json
import subprocess
import threading
import time

import pytest

from collector import control, lock, main
from collector import publish as pub
from tests.test_publish import make_config, seed

ENGINES = ["ookla", "cloudflare"]


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def request(req_id="web-1", test="speed", engine="auto", at=None, by="mick"):
    return {"id": req_id, "test": test, "engine": engine, "by": by,
            "requested_at": int(time.time()) if at is None else at}


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def test_parse_accepts_a_well_formed_request():
    req = control.parse_request(request(engine="cloudflare"), ENGINES)
    assert req["id"] == "web-1" and req["test"] == "speed" and req["engine"] == "cloudflare"
    assert req["by"] == "mick"


@pytest.mark.parametrize("bad", [
    {"id": "../../etc", "test": "speed", "requested_at": 1},
    {"id": "x; rm -rf /", "test": "speed", "requested_at": 1},
    {"id": "ok", "test": "shell", "requested_at": 1},
    {"id": "ok", "test": "speed", "requested_at": "soon"},
    {"id": "ok", "test": "speed"},
    ["not", "a", "mapping"],
])
def test_parse_rejects_anything_unexpected(bad):
    assert control.parse_request(bad, ENGINES) is None


def test_parse_falls_back_to_rotation_for_auto_or_unconfigured_engines():
    assert control.parse_request(request(engine="auto"), ENGINES)["engine"] is None
    assert control.parse_request(request(engine="speedtest-cli"), ENGINES)["engine"] is None
    assert control.parse_request(request(engine="$(reboot)"), ENGINES)["engine"] is None


def test_parse_drops_a_requester_name_that_is_not_a_github_login():
    assert control.parse_request(request(by="<script>"), ENGINES)["by"] is None


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------
def fresh_state():
    return {"handled": [], "recent": []}


def test_select_runs_a_fresh_request_once():
    state = fresh_state()
    chosen, skipped = control.select([request()], state, ENGINES, 900, int(time.time()))
    assert chosen["id"] == "web-1" and skipped == []
    control.record(state, chosen)
    chosen, skipped = control.select([request()], state, ENGINES, 900, int(time.time()))
    assert chosen is None and skipped == []


def test_select_expires_stale_requests_instead_of_running_them():
    now = int(time.time())
    chosen, skipped = control.select(
        [request(at=now - 3600)], fresh_state(), ENGINES, 900, now
    )
    assert chosen is None
    assert [s["outcome"] for s in skipped] == ["expired"]


def test_select_runs_the_newest_and_marks_the_rest_superseded():
    now = int(time.time())
    reqs = [request("old", at=now - 60), request("new", at=now - 5)]
    chosen, skipped = control.select(reqs, fresh_state(), ENGINES, 900, now)
    assert chosen["id"] == "new"
    assert skipped[0]["id"] == "old" and skipped[0]["outcome"] == "superseded"


def test_state_round_trips_and_stays_bounded(tmp_path):
    cfg = make_config(tmp_path)
    state = control.load_state(cfg)
    for i in range(control.HANDLED_KEPT + 10):
        control.record(state, {"id": f"r{i}", "test": "speed", "requested_at": i})
    control.save_state(cfg, state)
    again = control.load_state(cfg)
    assert len(again["handled"]) == control.HANDLED_KEPT
    assert [r["id"] for r in control.published_status(cfg)][0] == f"r{control.HANDLED_KEPT + 9}"
    assert len(control.published_status(cfg)) == control.RECENT_KEPT


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("remote,expected", [
    ("git@github.com:mmorrow24work/home-broadband.git", "mmorrow24work/home-broadband"),
    ("https://github.com/owner/repo.git", "owner/repo"),
    ("https://github.com/owner/repo", "owner/repo"),
    ("ssh://git@github.com/owner/my.repo.git", "owner/my.repo"),
    ("/srv/git/site.git", None),
    ("git@gitlab.com:owner/repo.git", None),
])
def test_github_repo(remote, expected):
    assert pub.github_repo(remote) == expected


def test_control_info_only_for_github_and_when_enabled(tmp_path):
    cfg = make_config(tmp_path)
    cfg.data["publish"]["remote"] = "git@github.com:o/r.git"
    info = pub.control_info(cfg)
    assert info["repo"] == "o/r" and info["workflow"] == "run-test.yml"
    cfg.data["control"]["enabled"] = False
    assert pub.control_info(cfg) is None


# ---------------------------------------------------------------------------
# locking
# ---------------------------------------------------------------------------
def test_lock_is_exclusive_and_released(tmp_path):
    path = tmp_path / "speed.lock"
    with lock.held(path), pytest.raises(lock.Busy), lock.held(path, wait=0.2, poll=0.05):
        pass
    with lock.held(path):  # free again
        pass


def test_on_demand_speed_waits_for_a_running_test(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    ran = []
    monkeypatch.setattr(main, "_speed_locked", lambda *a: (ran.append(1), (0, "ok"))[1])
    release = threading.Event()

    def hold():
        with lock.held(main._lock_path(cfg, "speed")):
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    time.sleep(0.2)
    # a scheduled test gives way at once
    assert main.run_speed(cfg)[1].startswith("skipped")
    assert ran == []
    release.set()
    holder.join()
    assert main.run_speed(cfg, on_demand=True) == (0, "ok")


# ---------------------------------------------------------------------------
# end to end, against a local bare repo standing in for GitHub
# ---------------------------------------------------------------------------
@pytest.fixture()
def remote(tmp_path):
    path = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "gh-pages", str(path))
    return path


def put_requests(tmp_path, remote, files):
    work = tmp_path / "requester"
    if work.exists():
        import shutil
        shutil.rmtree(work)
    work.mkdir()
    git("init", "-q", "-b", "control", cwd=work)
    (work / "requests").mkdir()
    for name, payload in files.items():
        (work / "requests" / f"{name}.json").write_text(json.dumps(payload))
    git("add", "-A", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "req", cwd=work)
    git("push", "-q", "-f", str(remote), "HEAD:refs/heads/control", cwd=work)


def collector_cfg(tmp_path, remote):
    root = tmp_path / "pi"
    (root / "site").mkdir(parents=True)
    (root / "site" / "index.html").write_text("<h1>dash</h1>")
    cfg = make_config(
        root,
        publish={
            "enabled": True, "remote": str(remote), "branch": "gh-pages",
            "repo_dir": str(root), "work_dir": str(root / "work"),
            "collector_id": "pi", "sync_code": False,
            "git_user_name": "t", "git_user_email": "t@example.com",
        },
    )
    seed(cfg, int(time.time()), minutes=10)
    return cfg


def run_control(cfg):
    return main.cmd_control(cfg, argparse.Namespace(dry_run=False, no_publish=False))


def test_no_control_branch_means_nothing_to_do(tmp_path, remote):
    cfg = collector_cfg(tmp_path, remote)
    assert control.fetch_requests(cfg) == []
    assert run_control(cfg) == 0


def test_requested_test_runs_once_and_is_published(tmp_path, remote, monkeypatch):
    cfg = collector_cfg(tmp_path, remote)
    calls = []

    def fake_speed(cfg, engine=None, **kw):
        calls.append((engine, kw))
        return 0, "cloudflare: 498.0 down / 71.0 up Mbps"

    monkeypatch.setattr(main, "run_speed", fake_speed)
    put_requests(tmp_path, remote, {
        "pi": request("web-abc", engine="cloudflare"),
        "someone-else": request("web-not-mine"),
    })

    assert run_control(cfg) == 0
    assert calls == [("cloudflare", {"on_demand": True, "dry_run": False})]

    manifest = json.loads(git("--git-dir", str(remote), "show", "gh-pages:data/pi/manifest.json"))
    [entry] = manifest["on_demand"]
    assert entry["id"] == "web-abc" and entry["outcome"] == "ok"
    assert entry["detail"].startswith("cloudflare: 498.0")
    assert manifest["control"] is None  # a local path is not a GitHub repo

    # fetched again a minute later: already handled, nothing runs
    assert run_control(cfg) == 0
    assert len(calls) == 1


def test_request_for_all_collectors_is_picked_up(tmp_path, remote, monkeypatch):
    cfg = collector_cfg(tmp_path, remote)
    sweeps = []
    monkeypatch.setattr(main, "cmd_latency", lambda cfg, args: sweeps.append(1) or 0)
    monkeypatch.setattr(main, "run_speed", lambda *a, **k: pytest.fail("speed not asked for"))
    put_requests(tmp_path, remote, {"_all": request("web-all", test="latency")})

    assert main.cmd_control(cfg, argparse.Namespace(dry_run=False, no_publish=True)) == 0
    assert sweeps == [1]
    assert control.published_status(cfg)[0]["outcome"] == "ok"


def test_failed_test_is_reported_not_retried(tmp_path, remote, monkeypatch):
    cfg = collector_cfg(tmp_path, remote)
    monkeypatch.setattr(main, "run_speed", lambda *a, **k: (1, "ookla failed: no binary"))
    put_requests(tmp_path, remote, {"pi": request("web-fail")})

    assert main.cmd_control(cfg, argparse.Namespace(dry_run=False, no_publish=True)) == 1
    [entry] = control.published_status(cfg)
    assert entry["outcome"] == "failed" and "no binary" in entry["detail"]
    assert main.cmd_control(cfg, argparse.Namespace(dry_run=False, no_publish=True)) == 0
