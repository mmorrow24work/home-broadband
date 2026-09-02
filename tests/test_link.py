"""A 92 Mbps result on a 100 Mbit NIC is the Pi's limit, not the ISP's."""

from collector import link


def test_no_caveat_when_link_speed_is_unknown(monkeypatch):
    monkeypatch.setattr(link, "link_speed_mbps", lambda *a, **k: None)
    assert link.warn_if_nic_bound(92.3, 92.2) is None


def test_caveat_when_result_sits_at_a_fast_ethernet_ceiling(monkeypatch):
    monkeypatch.setattr(link, "link_speed_mbps", lambda *a, **k: 100)
    caveat = link.warn_if_nic_bound(92.3, 92.2)
    assert caveat is not None
    assert "100 Mbit" in caveat
    assert "not by" in caveat  # names the Pi, not the broadband


def test_upload_alone_can_trigger_the_caveat(monkeypatch):
    monkeypatch.setattr(link, "link_speed_mbps", lambda *a, **k: 100)
    assert link.warn_if_nic_bound(40.0, 94.0) is not None


def test_no_caveat_well_below_the_ceiling(monkeypatch):
    monkeypatch.setattr(link, "link_speed_mbps", lambda *a, **k: 1000)
    assert link.warn_if_nic_bound(468.8, 71.2) is None


def test_no_caveat_without_a_result(monkeypatch):
    monkeypatch.setattr(link, "link_speed_mbps", lambda *a, **k: 100)
    assert link.warn_if_nic_bound(None, None) is None


def test_bogus_kernel_speeds_are_rejected(monkeypatch, tmp_path):
    """The kernel reports -1 for unknown and silly values for virtual devices."""
    iface = tmp_path / "eth0"
    iface.mkdir()
    monkeypatch.setattr(link, "SYS_NET", tmp_path)

    for raw, expected in (("-1", None), ("0", None), ("100", 100), ("4294967295", None)):
        (iface / "speed").write_text(raw)
        assert link.link_speed_mbps("eth0") == expected


def test_wireless_reports_unknown_rather_than_a_false_ceiling(monkeypatch, tmp_path):
    iface = tmp_path / "wlan0"
    (iface / "wireless").mkdir(parents=True)
    (iface / "speed").write_text("300")
    monkeypatch.setattr(link, "SYS_NET", tmp_path)
    assert link.link_speed_mbps("wlan0") is None
