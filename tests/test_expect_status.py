"""expect_status: a healthy endpoint often answers an unauthenticated probe
with 401/403/404. Treating those as failures gives a permanently red target
that says nothing when the service actually breaks."""

import pytest

from collector.probes.http import DEFAULT_EXPECT, parse_expect, status_matches


def test_default_is_success_and_redirects():
    ranges = parse_expect(None)
    assert ranges == [(200, 399)]
    assert status_matches(200, ranges)
    assert status_matches(301, ranges)
    assert not status_matches(403, ranges)
    assert not status_matches(502, ranges)


@pytest.mark.parametrize(
    "spec",
    [200, "200", [200], "200-200"],
    ids=["int", "str", "list", "range"],
)
def test_equivalent_spellings(spec):
    assert status_matches(200, parse_expect(spec))
    assert not status_matches(201, parse_expect(spec))


def test_anthropic_endpoints_are_up_despite_non_2xx():
    """api.anthropic.com answers 404 at / and claude.ai answers 403 to a bare
    request. Both prove the edge is reachable; a 5xx does not."""
    ranges = parse_expect([200, 400, 401, 403, 404, 405])
    for status in (200, 403, 404):
        assert status_matches(status, ranges), status
    for status in (500, 502, 521, 522, 530):
        assert not status_matches(status, ranges), status


def test_mixed_ints_and_ranges():
    ranges = parse_expect(["200-299", 404, "429"])
    assert status_matches(204, ranges)
    assert status_matches(404, ranges)
    assert status_matches(429, ranges)
    assert not status_matches(300, ranges)


def test_default_constant_is_what_it_claims():
    assert parse_expect(DEFAULT_EXPECT) == [(200, 399)]


@pytest.mark.parametrize("bad", ["abc", "200-", ["x"], "2xx"])
def test_nonsense_is_rejected_loudly(bad):
    with pytest.raises(ValueError):
        parse_expect(bad)


def test_config_rejects_a_bad_expect_status(tmp_path):
    import yaml

    from collector.config import ConfigError, load_config

    data = {
        "publish": {"enabled": False},
        "latency": {
            "targets": [
                {"name": "A", "host": "example.com", "checks": ["http"],
                 "url": "https://example.com/", "expect_status": "2xx"}
            ]
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="expect_status"):
        load_config(path)


def test_config_accepts_a_good_expect_status(tmp_path):
    import yaml

    from collector.config import load_config

    data = {
        "publish": {"enabled": False},
        "latency": {
            "targets": [
                {"name": "Claude API", "host": "api.anthropic.com", "checks": ["http"],
                 "url": "https://api.anthropic.com/", "expect_status": [200, 404]}
            ]
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    cfg = load_config(path)
    assert cfg.targets[0]["expect_status"] == [200, 404]
