"""The debug SDR probe's verdict — the S0 spike's one piece of judgement.

The route itself is a thin proxy to the supervisor; what needs testing is the
translation from a raw USB scan into "can this box drive an SDR yet, and if not,
what is in the way?" — because that answer is what the owner reads instead of a
terminal.
"""

from jbrain.api.debug import _sdr_verdict

NESDR = {
    "name": "1-2",
    "usb_id": "0bda:2838",
    "manufacturer": "Nooelec",
    "product": "NESDR SMArt v5",
    "serial": "00000001",
    "device_node": "/dev/bus/usb/001/007",
    "drivers": [],
}


def _scan(sdrs: list[dict[str, object]], *, devices: int = 4, readable: bool = True):
    return {
        "sysfs_readable": readable,
        "devices": [{"usb_id": f"dead:{i:04d}"} for i in range(devices)],
        "sdrs": sdrs,
    }


def test_unclaimed_dongle_is_ready_and_names_the_node_to_pass_through() -> None:
    out = _sdr_verdict(_scan([NESDR]))

    assert out.found and out.ready
    assert "NESDR SMArt v5" in out.summary
    assert "0bda:2838" in out.summary
    assert "/dev/bus/usb/001/007" in out.next_step
    assert out.sdrs[0].claimed_by_dvb is False


def test_dvb_driver_claim_is_found_but_not_ready_and_names_the_blacklist() -> None:
    claimed = {**NESDR, "drivers": ["dvb_usb_rtl28xxu"]}

    out = _sdr_verdict(_scan([claimed]))

    assert out.found is True
    assert out.ready is False  # the whole point: present != usable
    assert "dvb_usb_rtl28xxu" in out.summary
    assert "Blacklist dvb_usb_rtl28xxu" in out.next_step
    assert out.sdrs[0].claimed_by_dvb is True


def test_a_non_dvb_driver_claim_is_reported_without_the_blacklist_advice() -> None:
    claimed = {**NESDR, "drivers": ["usbfs"]}

    out = _sdr_verdict(_scan([claimed]))

    assert out.found and not out.ready
    assert "usbfs" in out.summary
    assert "Blacklist" not in out.next_step
    assert out.sdrs[0].claimed_by_dvb is False


def test_no_sdr_still_proves_the_scan_ran() -> None:
    out = _sdr_verdict(_scan([], devices=9))

    assert not out.found and not out.ready
    assert "9 USB device(s)" in out.summary  # distinguishes "absent" from "blind"
    assert out.usb_device_count == 9


def test_unreadable_sysfs_reports_cannot_tell_rather_than_not_found() -> None:
    out = _sdr_verdict(_scan([], devices=0, readable=False))

    assert out.found is False
    assert out.ready is False
    assert "Cannot tell" in out.summary
    assert out.sysfs_readable is False


def test_falls_back_to_the_usb_id_when_the_device_has_no_product_string() -> None:
    anonymous = {**NESDR, "product": None}

    out = _sdr_verdict(_scan([anonymous]))

    assert "0bda:2838" in out.summary


def test_missing_device_node_still_gives_actionable_advice() -> None:
    no_node = {**NESDR, "device_node": None}

    out = _sdr_verdict(_scan([no_node]))

    assert out.ready is True
    assert "/dev/bus/usb" in out.next_step
