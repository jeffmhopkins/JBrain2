"""The probe's PROSE, which is the part an owner with no terminal actually reads.

`sdrs` was always a correct list. Every sentence built from it described `sdrs[0]` as
though it were the whole picture — so the day a second dongle was plugged in, the
summary line said "Found NESDR SMArt v5" while two were attached, and `next_step` named
one serial as if it were the choice. On a box operated entirely through the PWA and this
API (CLAUDE.md #10), a summary that quietly undercounts the hardware is the failure that
matters, not the list nobody scrolls to.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx

from jbrain.api.debug import _sdr_verdict, sdr_probe


def _dongle(serial: str, node: str, **over: Any) -> dict[str, Any]:
    return {
        "name": "1-1",
        "usb_id": "0bda:2838",
        "product": "NESDR SMArt v5",
        "serial": serial,
        "device_node": node,
        "drivers": [],
        "claimed_by_dvb": False,
        **over,
    }


def _scan(*sdrs: dict[str, Any]) -> dict[str, Any]:
    return {"sysfs_readable": True, "devices": list(sdrs), "sdrs": list(sdrs)}


def test_one_dongle_reads_as_one() -> None:
    out = _sdr_verdict(_scan(_dongle("09022796", "/dev/bus/usb/001/011")))

    assert out.ready is True
    assert "and 1 other" not in out.summary
    assert "09022796" in out.next_step


def test_two_dongles_are_both_counted_and_both_named() -> None:
    out = _sdr_verdict(
        _scan(
            _dongle("09022796", "/dev/bus/usb/001/011"),
            _dongle("77192819", "/dev/bus/usb/003/010", name="3-4"),
        )
    )

    assert out.ready is True
    assert len(out.sdrs) == 2
    assert "and 1 other" in out.summary
    # Both serials, because with two attached naming one reads as naming THE one.
    assert "09022796" in out.next_step
    assert "77192819" in out.next_step


def test_two_dongles_say_that_the_choice_between_them_is_not_a_setting() -> None:
    """The reason this matters, spelled out where the operator is looking.

    `rtl_fm` and `rtl_power` are invoked with no `-d`, so they open whichever librtlsdr
    enumerates first. Two radios on two antennas and a silent choice between them is how
    APRS ends up on the wrong one with no symptom but worse reception."""
    one = _sdr_verdict(_scan(_dongle("09022796", "/dev/bus/usb/001/011")))
    two = _sdr_verdict(
        _scan(
            _dongle("09022796", "/dev/bus/usb/001/011"),
            _dongle("77192819", "/dev/bus/usb/003/010", name="3-4"),
        )
    )

    assert "enumeration order" in two.next_step
    # ...and it is not said when there is nothing to be ambiguous about.
    assert "enumeration order" not in one.next_step


def test_a_dvb_claimed_dongle_still_counts_the_others() -> None:
    out = _sdr_verdict(
        _scan(
            _dongle(
                "09022796",
                "/dev/bus/usb/001/011",
                drivers=["dvb_usb_rtl28xxu"],
                claimed_by_dvb=True,
            ),
            _dongle("77192819", "/dev/bus/usb/003/010", name="3-4"),
        )
    )

    assert out.ready is False
    assert "and 1 other" in out.summary


def test_a_dongle_held_by_something_else_still_counts_the_others() -> None:
    """The OTHER not-ready branch. `usbfs` is what holds a dongle a running pipeline has
    open, so this is the state the box is in most of the time it is working — and it
    reported one radio while two were plugged in just as readily as the DVB branch."""
    out = _sdr_verdict(
        _scan(
            _dongle("09022796", "/dev/bus/usb/001/011", drivers=["usbfs"]),
            _dongle("77192819", "/dev/bus/usb/003/010", name="3-4"),
        )
    )

    assert out.ready is False
    assert "usbfs" in out.summary
    assert "and 1 other" in out.summary


async def test_a_scan_that_does_not_answer_is_an_answer_not_a_500() -> None:
    """MEASURED 2026-09-04: a USB port reset made the bus read slow enough to time out,
    and this route replied with a 500 and a traceback — during the exact minute the owner
    most needed to know what was on the bus, and with no terminal to look with
    (CLAUDE.md #10). "Cannot tell" is a real state this shape already carries."""

    class _Unreachable:
        async def get(self, _path: str, **_kw: Any) -> Any:
            raise httpx.ReadTimeout("the bus is busy re-enumerating")

    request = SimpleNamespace(
        state=SimpleNamespace(),
        app=SimpleNamespace(state=SimpleNamespace(supervisor_client=_Unreachable())),
    )

    out = await sdr_probe(request, SimpleNamespace(supervisor_token="t"), object())  # type: ignore[arg-type]

    assert out.ready is False
    assert out.sysfs_readable is False
    assert "Cannot tell" in out.summary
    # And it says the one thing that is actually true of a reset in progress.
    assert "re-enumerating" in out.next_step
