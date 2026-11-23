"""Two collectors, one site: each owns data/<id>/ and keeps the other's."""

import json
import subprocess
import time

import pytest

from collector import publish as pub
from tests.test_publish import make_config, seed


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def remote(tmp_path):
    path = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "gh-pages", str(path))
    return path


def collector(tmp_path, name, remote, **site):
    root = tmp_path / name
    root.mkdir()
    (root / "site").mkdir()
    (root / "site" / "index.html").write_text("<h1>dash</h1>")
    cfg = make_config(
        root,
        site={"title": "T", "timezone": "Europe/London", "collector": name.upper(), **site},
        publish={
            "enabled": True, "remote": str(remote), "branch": "gh-pages",
            "repo_dir": str(root), "work_dir": str(root / "work"),
            "collector_id": name, "sync_code": False,
            "git_user_name": "t", "git_user_email": "t@example.com",
        },
    )
    seed(cfg, int(time.time()), minutes=10)
    return cfg


def published(remote, path):
    return git("--git-dir", str(remote), "show", f"gh-pages:{path}")


def listing(remote, path="data"):
    return set(git("--git-dir", str(remote), "ls-tree", "--name-only", f"gh-pages:{path}").split())


def test_collector_id_is_a_slug(tmp_path):
    cfg = make_config(tmp_path)
    cfg.data["publish"]["collector_id"] = "Mick's PC (Wi-Fi)"
    assert pub.collector_id(cfg) == "mick-s-pc-wi-fi"
    cfg.data["publish"]["collector_id"] = "%%"
    assert pub.collector_id(cfg) == "collector"


def test_two_collectors_share_the_branch(tmp_path, remote):
    tegra = collector(tmp_path, "tegra", remote, domain="bb.example.com")
    pc = collector(tmp_path, "pc", remote, domain="bb.example.com")

    assert pub.publish(tegra) == 0
    assert pub.publish(pc) == 0
    assert listing(remote) == {"tegra", "pc", "collectors.json"}

    # and again the other way round: tegra must keep pc's data too
    assert pub.publish(tegra) == 0
    assert listing(remote) == {"tegra", "pc", "collectors.json"}
    assert git("--git-dir", str(remote), "rev-list", "--count", "gh-pages").strip() == "1"

    index = json.loads(published(remote, "data/collectors.json"))
    assert {c["id"] for c in index["collectors"]} == {"tegra", "pc"}
    assert {c["label"] for c in index["collectors"]} == {"TEGRA", "PC"}
    manifest = json.loads(published(remote, "data/pc/manifest.json"))
    assert manifest["id"] == "pc"
    assert published(remote, "CNAME").strip() == "bb.example.com"


def test_old_single_collector_layout_is_replaced(tmp_path, remote):
    seed_repo = tmp_path / "legacy"
    git("init", "-q", "-b", "gh-pages", str(seed_repo))
    (seed_repo / "data" / "daily").mkdir(parents=True)
    (seed_repo / "data" / "manifest.json").write_text("{}")
    (seed_repo / "data" / "daily" / "2026-01-01.json").write_text("{}")
    git("-C", str(seed_repo), "add", "-A")
    git("-C", str(seed_repo), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "old")
    git("-C", str(seed_repo), "push", "-q", str(remote), "gh-pages")

    assert pub.publish(collector(tmp_path, "tegra", remote)) == 0
    assert listing(remote) == {"tegra", "collectors.json"}


def test_collector_without_domain_keeps_cname(tmp_path, remote):
    assert pub.publish(collector(tmp_path, "tegra", remote, domain="bb.example.com")) == 0
    assert pub.publish(collector(tmp_path, "pc", remote, domain="")) == 0
    assert published(remote, "CNAME").strip() == "bb.example.com"


def test_drop_collectors_removes_a_retired_one(tmp_path, remote):
    assert pub.publish(collector(tmp_path, "oldpi", remote)) == 0
    tegra = collector(tmp_path, "tegra", remote)
    tegra.data["publish"]["drop_collectors"] = ["oldpi"]
    assert pub.publish(tegra) == 0
    assert listing(remote) == {"tegra", "collectors.json"}


def test_push_retries_when_another_collector_wins_the_race(tmp_path, remote, monkeypatch):
    tegra = collector(tmp_path, "tegra", remote)
    pc = collector(tmp_path, "pc", remote)
    assert pub.publish(tegra) == 0

    real_fetch = pub._fetch_published
    calls = []

    def fetch_then_lose(repo, branch, env):
        base = real_fetch(repo, branch, env)
        if not calls:  # tegra publishes between pc's fetch and pc's push
            monkeypatch.setattr(pub, "_fetch_published", real_fetch)
            tegra.data["site"]["title"] = "changed"  # a different snapshot
            assert pub.publish(tegra) == 0
            monkeypatch.setattr(pub, "_fetch_published", fetch_then_lose)
        calls.append(base)
        return base

    monkeypatch.setattr(pub, "_fetch_published", fetch_then_lose)
    monkeypatch.setattr(pub.time, "sleep", lambda s: None)
    assert pub.publish(pc) == 0
    assert len(calls) == 2 and calls[0] != calls[1]
    assert listing(remote) == {"tegra", "pc", "collectors.json"}


def test_index_puts_wired_collectors_first(tmp_path):
    root = tmp_path / "data"
    for cid, conn in (("a-laptop", "wifi"), ("z-box", "wired")):
        (root / cid).mkdir(parents=True)
        (root / cid / "manifest.json").write_text(json.dumps(
            {"generated_at": 1, "host": {"label": cid, "connection": conn}}
        ))
    (root / "stray").mkdir()
    entries = pub.write_collectors_index(root)
    assert [e["id"] for e in entries] == ["z-box", "a-laptop"]
