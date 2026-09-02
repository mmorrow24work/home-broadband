"""Cloudflare's /meta now returns {} — the edge details come from /cdn-cgi/trace."""

from collector.probes.cloudflare import parse_trace

TRACE = """fl=123f456
h=speed.cloudflare.com
ip=203.0.113.45
ts=1756848000.123
visit_scheme=https
uag=python-requests/2.31.0
colo=LHR
sliver=none
http=http/2
loc=GB
tls=TLSv1.3
sni=plaintext
warp=off
gateway=off
rbi=off
kex=X25519
"""


def test_extracts_the_fields_that_identify_the_edge():
    fields = parse_trace(TRACE)
    assert fields["colo"] == "LHR"
    assert fields["loc"] == "GB"
    assert fields["ip"] == "203.0.113.45"
    assert fields["http"] == "http/2"


def test_values_containing_equals_are_kept_whole():
    assert parse_trace("ts=1756848000.123\nfoo=a=b\n")["foo"] == "a=b"


def test_blank_and_malformed_lines_are_skipped():
    fields = parse_trace("colo=LHR\n\ngarbage\nloc=GB\n")
    assert fields == {"colo": "LHR", "loc": "GB"}


def test_empty_body_yields_nothing():
    assert parse_trace("") == {}
