"""The installers show only the setup steps `setup-check` says are left."""

from collector import link
from collector import publish as pub
from collector import setup_check as sc
from tests.test_multi_collector import collector, remote  # noqa: F401 - fixture
from tests.test_publish import make_config

ROUTES = """\
default via 192.168.1.5 dev enP1p5s0 proto dhcp metric 100
default via 192.168.55.100 dev l4tbr0 metric 32766 linkdown
"""


def test_gateway_follows_the_route_the_kernel_uses():
    assert link.parse_default_gateway(ROUTES) == "192.168.1.5"
    assert link.parse_default_gateway("default dev wg0 scope link") is None
    assert link.parse_default_gateway("") is None


def test_site_url_from_domain_or_github_remote(tmp_path):
    cfg = make_config(tmp_path)
    cfg.data["site"]["domain"] = "bb.example.com"
    assert sc.site_url(cfg) == "https://bb.example.com/"
    cfg.data["site"]["domain"] = ""
    cfg.data["publish"]["remote"] = "git@github.com:Someone/home-broadband.git"
    assert sc.site_url(cfg) == "https://someone.github.io/home-broadband/"


def test_config_problems(tmp_path):
    cfg = make_config(tmp_path)  # Router 192.168.1.1, speeds 500/0/250
    assert sc.config_problems(cfg, "192.168.1.1") == []
    problems = sc.config_problems(cfg, "192.168.1.254")
    assert problems == [
        "the Router target is 192.168.1.1, but this machine's gateway is 192.168.1.254"
    ]
    cfg.data["site"]["isp"].update(advertised_up_mbps=70)
    assert any("example speeds" in p for p in sc.config_problems(cfg, None))
    cfg.data["publish"].update(enabled=True, remote="")
    assert any("publish.remote is empty" in p for p in sc.config_problems(cfg, None))


def test_first_run_needs_key_and_smoke_test(tmp_path, remote, monkeypatch):  # noqa: F811
    cfg = collector(tmp_path, "pc", remote)
    monkeypatch.setattr(link, "default_gateway", lambda: "192.168.1.1")
    assert [s for s, _ in sc.pending(cfg)] == ["key", "smoke"]


def test_after_a_publish_only_a_missing_site_is_left(tmp_path, remote, monkeypatch):  # noqa: F811
    cfg = collector(tmp_path, "pc", remote, domain="bb.example.com")
    assert pub.publish(cfg) == 0
    assert sc.has_published(cfg)
    monkeypatch.setattr(link, "default_gateway", lambda: "192.168.1.1")

    monkeypatch.setattr(sc, "site_lists_collector", lambda cfg, url: False)
    assert sc.pending(cfg) == [("pages", "https://bb.example.com/")]

    monkeypatch.setattr(sc, "site_lists_collector", lambda cfg, url: True)
    assert sc.pending(cfg) == [("ok", "https://bb.example.com/")]

    # unreachable (offline installer run) is not reported as a problem
    monkeypatch.setattr(sc, "site_lists_collector", lambda cfg, url: None)
    assert sc.pending(cfg) == [("ok", "https://bb.example.com/")]
