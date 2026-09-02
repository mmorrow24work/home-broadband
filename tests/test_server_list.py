"""Server selection follows speedtest.net's geolocation of your IP, which is
often wrong on carrier ranges. Parsing the list is what lets you override it."""

import pytest

from collector.probes.speedtest_cli import parse_server_list

# Real shape of `speedtest-cli --list` on a UK line geolocated to the Netherlands.
SAMPLE = """Retrieving speedtest.net configuration...
74684) Digi Turunc (Amsterdam, Netherlands) [539.73 km]
52365) Odido (Amsterdam, Netherlands) [540.39 km]
 5807) Vodafone UK (Birmingham, United Kingdom) [563.10 km]
30595) Community Fibre (London, United Kingdom) [571.02 km]
12907) Zen Internet (Manchester, United Kingdom) [601.44 km]
"""


def test_parses_every_server_row():
    servers = parse_server_list(SAMPLE)
    assert len(servers) == 5
    assert [s["id"] for s in servers][:2] == ["74684", "52365"]


def test_splits_city_from_country():
    servers = parse_server_list(SAMPLE)
    uk = [s for s in servers if s["country"] == "United Kingdom"]
    assert [s["city"] for s in uk] == ["Birmingham", "London", "Manchester"]
    assert uk[0]["sponsor"] == "Vodafone UK"
    assert uk[0]["km"] == pytest.approx(563.10)


def test_ignores_the_preamble_and_blank_lines():
    assert parse_server_list("Retrieving speedtest.net configuration...\n\n") == []


def test_sponsor_names_containing_brackets_and_dots():
    text = "41423) BlackHOST Ltd. (Amsterdam, Netherlands) [540.39 km]\n"
    server = parse_server_list(text)[0]
    assert server["sponsor"] == "BlackHOST Ltd."
    assert server["city"] == "Amsterdam"


def test_location_without_a_city():
    server = parse_server_list("999) Someone (Singapore) [10.00 km]\n")[0]
    assert server["city"] == "Singapore"
    assert server["country"] == "Singapore"


def test_filtering_by_country_beats_trusting_the_distance_order():
    """The nearest listed server is in the wrong country — the point of the
    subcommand is that you can still find a local one."""
    servers = parse_server_list(SAMPLE)
    assert servers[0]["country"] == "Netherlands"
    uk = [s for s in servers if "united kingdom" in s["country"].lower()]
    assert uk and uk[0]["id"] == "5807"
