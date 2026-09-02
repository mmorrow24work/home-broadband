import json
import time

import yaml

from collector import db
from collector.config import load_config
from collector.publish import _percentile, build_export, find_outages, window_summary


def make_config(tmp_path, **overrides):
    data = {
        "site": {
            "title": "Test",
            "timezone": "Europe/London",
            "isp": {"advertised_down_mbps": 500, "guaranteed_min_down_mbps": 250},
        },
        "database": {"path": str(tmp_path / "test.db")},
        "publish": {
            "enabled": False,
            "bucket_seconds": 300,
            "latest_hours": 48,
            "repo_dir": str(tmp_path),
            "work_dir": str(tmp_path / "work"),
        },
        "latency": {
            "interval_seconds": 60,
            "targets": [
                {"name": "Router", "host": "192.168.1.1", "group": "lan"},
                {"name": "CF", "host": "1.1.1.1", "group": "internet", "primary": True},
                {"name": "Google", "host": "8.8.8.8", "group": "internet"},
            ],
        },
    }
    data.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return load_config(path)


def seed(cfg, now, minutes=180, outage_at=None, outage_len=5):
    db.init(cfg.db_path)
    with db.connect(cfg.db_path) as conn:
        rows = []
        for i in range(minutes):
            ts = now - (minutes - i) * 60
            in_outage = outage_at is not None and outage_at <= i < outage_at + outage_len
            for name, base in (("Router", 0.8), ("CF", 9.0), ("Google", 12.0)):
                wan_down = in_outage and name != "Router"
                rows.append(
                    {
                        "ts": ts, "target": name, "host": "x", "family": "ipv4",
                        "sent": 5, "recv": 0 if wan_down else 5,
                        "loss_pct": 100.0 if wan_down else 0.0,
                        "rtt_min": None if wan_down else base,
                        "rtt_avg": None if wan_down else base + (i % 5) * 0.1,
                        "rtt_max": None if wan_down else base + 2,
                        "rtt_mdev": None if wan_down else 0.3,
                        "error": None,
                    }
                )
        db.insert_latency(conn, rows)

        for i, down in enumerate([480.0, 512.0, 240.0, 495.0]):
            db.insert_speed(
                conn,
                {
                    "ts": now - (4 - i) * 1800, "engine": "ookla" if i % 2 == 0 else "cloudflare",
                    "ok": 1, "down_mbps": down, "up_mbps": 70.0, "ping_ms": 9.0,
                    "jitter_ms": 0.5, "loss_pct": 0.0, "server": "London", "server_id": "1",
                    "isp": "Test ISP", "external_ip": "203.0.113.1", "result_url": None,
                    "bytes_down": 700_000_000, "bytes_up": 100_000_000, "duration_s": 24.0,
                    "error": None, "raw": None,
                },
            )


def test_percentile_uses_nearest_rank():
    values = list(range(1, 101))
    assert _percentile(values, 50) == 50
    assert _percentile(values, 95) == 95
    assert _percentile([], 95) is None
    assert _percentile([None, 5.0], 50) == 5.0


def test_outage_detected_only_when_every_wan_target_is_down(tmp_path):
    now = int(time.time())
    cfg = make_config(tmp_path)
    seed(cfg, now, outage_at=60, outage_len=5)
    with db.connect(cfg.db_path, readonly=True) as conn:
        outages = find_outages(conn, cfg, now - 86400, now + 1)
    assert len(outages) == 1
    assert outages[0]["seconds"] == 5 * 60  # 5 sweeps at a 60s interval


def test_lan_only_failure_is_not_an_outage(tmp_path):
    """A dead gateway probe must not be reported as an internet outage."""
    now = int(time.time())
    cfg = make_config(tmp_path)
    seed(cfg, now)
    with db.connect(cfg.db_path) as conn:
        conn.execute("UPDATE latency SET loss_pct = 100.0 WHERE target = 'Router'")
        outages = find_outages(conn, cfg, now - 86400, now + 1)
    assert outages == []


def test_window_summary_numbers(tmp_path):
    now = int(time.time())
    cfg = make_config(tmp_path)
    seed(cfg, now, outage_at=60, outage_len=5)
    with db.connect(cfg.db_path, readonly=True) as conn:
        summary = window_summary(conn, cfg, now - 86400, now + 1)

    assert summary["tests"] == 4
    assert summary["down"]["min"] == 240.0
    assert summary["down"]["max"] == 512.0
    # one of four results sits below the 250 Mbps guaranteed minimum
    assert summary["below_guaranteed_pct"] == 25.0
    assert summary["latency"]["target"] == "CF"
    assert summary["outage_count"] == 1
    assert 0 < summary["availability_pct"] < 100
    assert summary["data_used_gb"] == 3.2


def test_build_export_writes_a_loadable_site_payload(tmp_path):
    now = int(time.time())
    cfg = make_config(tmp_path)
    seed(cfg, now, outage_at=30, outage_len=3)
    out = tmp_path / "out"
    out.mkdir()

    result = build_export(cfg, out)

    manifest = json.loads((out / "data" / "manifest.json").read_text())
    latest = json.loads((out / "data" / "latest.json").read_text())
    summary = json.loads((out / "data" / "summary.json").read_text())

    assert manifest["days"] == result["days"]
    assert [t["name"] for t in manifest["targets"]] == ["Router", "CF", "Google"]
    assert manifest["site"]["isp"]["name"] == "Test ISP"  # auto-detected from the test result

    # column-wise layout, equal lengths, newest last
    series = latest["latency"]["CF"]
    assert len(series["t"]) == len(series["rtt"]) == len(series["loss"])
    assert series["t"] == sorted(series["t"])
    assert latest["speed"]["down"][-1] == 495.0

    assert summary["windows"]["24h"]["tests"] == 4
    assert summary["current"]["down_mbps"] == 495.0

    day_files = list((out / "data" / "daily").glob("*.json"))
    assert day_files
    day = json.loads(day_files[-1].read_text())
    assert day["resolution"] == 300
    # 5-minute buckets must be coarser than the raw 60-second samples
    assert len(day["latency"]["CF"]["t"]) < len(latest["latency"]["CF"]["t"])


def test_export_is_reasonably_compact(tmp_path):
    """A day of 1-minute probes across 3 targets should stay well under 200 kB."""
    now = int(time.time())
    cfg = make_config(tmp_path)
    seed(cfg, now, minutes=1440)
    out = tmp_path / "out"
    out.mkdir()
    result = build_export(cfg, out)
    assert result["bytes"] < 400_000


def test_engines_rotate_round_robin(tmp_path):
    """Three engines must cycle evenly, and pick up where the last run left off."""
    now = int(time.time())
    cfg = make_config(tmp_path)
    engines = ["ookla", "speedtest-cli", "cloudflare"]
    db.init(cfg.db_path)

    with db.connect(cfg.db_path) as conn:
        assert db.next_engine(conn, engines) == "ookla"  # empty table starts at the front

        seen = []
        for i in range(7):
            engine = db.next_engine(conn, engines)
            seen.append(engine)
            db.insert_speed(conn, {
                "ts": now + i, "engine": engine, "ok": 1, "down_mbps": 400.0,
                "up_mbps": 60.0, "ping_ms": 9.0, "jitter_ms": None, "loss_pct": None,
                "server": None, "server_id": None, "isp": None, "external_ip": None,
                "result_url": None, "bytes_down": 1, "bytes_up": 1, "duration_s": 1.0,
                "error": None, "raw": None,
            })

    assert seen == ["ookla", "speedtest-cli", "cloudflare"] * 2 + ["ookla"]


def test_failed_run_still_advances_the_rotation(tmp_path):
    """A missing binary must not pin the rotation to one engine forever."""
    now = int(time.time())
    cfg = make_config(tmp_path)
    engines = ["ookla", "speedtest-cli", "cloudflare"]
    db.init(cfg.db_path)

    with db.connect(cfg.db_path) as conn:
        db.insert_speed(conn, {
            "ts": now, "engine": "ookla", "ok": 0, "down_mbps": None, "up_mbps": None,
            "ping_ms": None, "jitter_ms": None, "loss_pct": None, "server": None,
            "server_id": None, "isp": None, "external_ip": None, "result_url": None,
            "bytes_down": 0, "bytes_up": 0, "duration_s": None,
            "error": "'speedtest' not found on PATH", "raw": None,
        })
        assert db.next_engine(conn, engines) == "speedtest-cli"


def test_removing_an_engine_from_the_config_recovers(tmp_path):
    """After dropping 'ookla', a database full of ookla rows must not wedge."""
    now = int(time.time())
    cfg = make_config(tmp_path)
    db.init(cfg.db_path)
    with db.connect(cfg.db_path) as conn:
        db.insert_speed(conn, {
            "ts": now, "engine": "ookla", "ok": 1, "down_mbps": 400.0, "up_mbps": 60.0,
            "ping_ms": 9.0, "jitter_ms": None, "loss_pct": None, "server": None,
            "server_id": None, "isp": None, "external_ip": None, "result_url": None,
            "bytes_down": 1, "bytes_up": 1, "duration_s": 1.0, "error": None, "raw": None,
        })
        assert db.next_engine(conn, ["speedtest-cli", "cloudflare"]) == "speedtest-cli"


def test_manifest_records_the_collector_link_speed(tmp_path):
    """The dashboard needs this to caveat a result capped by the Pi's own NIC."""
    now = int(time.time())
    cfg = make_config(tmp_path)
    seed(cfg, now, minutes=10)
    out = tmp_path / "out"
    out.mkdir()
    build_export(cfg, out)

    manifest = json.loads((out / "data" / "manifest.json").read_text())
    assert "host" in manifest
    assert set(manifest["host"]) == {"interface", "link_speed_mbps"}


def test_copy_contents_ignores_source_permissions(tmp_path):
    """An --in-place checkout has setgid directories so two accounts can share
    the git repo. shutil.copytree tries to reproduce that on the destination and
    fails with EPERM; the publisher must copy contents only."""
    import os

    from collector.publish import copy_contents

    src = tmp_path / "site"
    (src / "vendor").mkdir(parents=True)
    (src / "vendor" / "uPlot.js").write_text("// js")
    (src / "index.html").write_text("<h1>hi</h1>")

    # setgid + a mode the copy must not try to replicate
    os.chmod(src / "vendor", 0o2775)
    os.chmod(src / "vendor" / "uPlot.js", 0o640)

    dst = tmp_path / "tree"
    copy_contents(src, dst)

    assert (dst / "index.html").read_text() == "<h1>hi</h1>"
    assert (dst / "vendor" / "uPlot.js").read_text() == "// js"
    # the setgid bit must NOT have been carried over
    assert not os.stat(dst / "vendor").st_mode & 0o2000


def test_copy_contents_is_recursive_and_overwrites(tmp_path):
    from collector.publish import copy_contents

    src = tmp_path / "a"
    (src / "one" / "two").mkdir(parents=True)
    (src / "one" / "two" / "deep.txt").write_text("new")

    dst = tmp_path / "b"
    (dst / "one" / "two").mkdir(parents=True)
    (dst / "one" / "two" / "deep.txt").write_text("old")

    copy_contents(src, dst)
    assert (dst / "one" / "two" / "deep.txt").read_text() == "new"
