"""Parser tests use captured real output — the formats vary more than you'd expect."""

from collector.probes.ping import build_command, parse_ping_output

IPUTILS_OK = """
PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.

--- 1.1.1.1 ping statistics ---
5 packets transmitted, 5 received, 0% packet loss, time 1004ms
rtt min/avg/max/mdev = 11.542/12.031/12.884/0.478 ms
"""

IPUTILS_PARTIAL_LOSS = """
--- 8.8.8.8 ping statistics ---
5 packets transmitted, 4 received, 20% packet loss, time 4056ms
rtt min/avg/max/mdev = 9.101/9.884/10.442/0.502 ms
"""

IPUTILS_TOTAL_LOSS = """
--- 192.168.1.99 ping statistics ---
5 packets transmitted, 0 received, 100% packet loss, time 4098ms
"""

IPUTILS_ERRORS = """
--- 10.0.0.1 ping statistics ---
5 packets transmitted, 3 received, +2 errors, 40% packet loss, time 4079ms
rtt min/avg/max/mdev = 1.204/1.560/1.988/0.325 ms
"""

BUSYBOX = """
--- 1.1.1.1 ping statistics ---
5 packets transmitted, 5 packets received, 0% packet loss
round-trip min/avg/max/stddev = 11.542/12.031/12.884/0.478 ms
"""

FRACTIONAL_LOSS = """
--- 9.9.9.9 ping statistics ---
1000 packets transmitted, 997 received, 0.3% packet loss, time 250100ms
rtt min/avg/max/mdev = 8.100/8.740/29.110/1.020 ms
"""


def test_clean_run():
    result = parse_ping_output(IPUTILS_OK)
    assert result["sent"] == 5
    assert result["recv"] == 5
    assert result["loss_pct"] == 0.0
    assert result["rtt_avg"] == 12.031
    assert result["rtt_max"] == 12.884


def test_partial_loss():
    result = parse_ping_output(IPUTILS_PARTIAL_LOSS)
    assert result["loss_pct"] == 20.0
    assert result["recv"] == 4
    assert result["rtt_avg"] == 9.884


def test_total_loss_has_no_rtt():
    result = parse_ping_output(IPUTILS_TOTAL_LOSS)
    assert result["loss_pct"] == 100.0
    assert result["recv"] == 0
    assert result["rtt_avg"] is None


def test_errors_line_does_not_break_the_counts():
    result = parse_ping_output(IPUTILS_ERRORS)
    assert (result["sent"], result["recv"], result["loss_pct"]) == (5, 3, 40.0)


def test_busybox_round_trip_wording():
    result = parse_ping_output(BUSYBOX)
    assert result["recv"] == 5
    assert result["rtt_avg"] == 12.031


def test_fractional_loss():
    assert parse_ping_output(FRACTIONAL_LOSS)["loss_pct"] == 0.3


def test_garbage_is_treated_as_unreachable():
    result = parse_ping_output("ping: connect: Network is unreachable")
    assert result["sent"] == 0
    assert result["loss_pct"] == 100.0


def test_command_selects_address_family():
    assert "-6" in build_command("2606:4700:4700::1111", family="ipv6")
    assert "-4" in build_command("1.1.1.1", family="ipv4")
    cmd = build_command("example.com", family="auto")
    assert "-4" not in cmd and "-6" not in cmd
    assert cmd[-1] == "example.com"
