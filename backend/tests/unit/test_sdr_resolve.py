"""Asking which radio, when the owner has already pointed at one.

`roles.py` holds the rules and is tested against them directly. This is the plumbing
around them, and what is worth testing here is the seam: which question gets asked, and
what happens when the USB scan cannot answer — because that last case is the one where
a wrong choice is invisible. The box opens *a* radio either way.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from jbrain.db.session import SessionContext
from jbrain.sdr import resolve
from jbrain.sdr.roles import GENERAL, Radio

WHIP = "09022796"
WIRE = "77192819"
CTX = SessionContext(principal_id="owner", principal_kind="owner")


def _client(usb: dict[str, Any] | None) -> Any:
    class _Client:
        async def get(self, _path: str, **_kw: Any) -> Any:
            if usb is None:
                raise httpx.ConnectError("no supervisor")
            return httpx.Response(200, json=usb, request=httpx.Request("GET", "http://s/usb"))

    return _Client()


def _store(*radios: Radio) -> Any:
    async def sdr_radios(_ctx: Any) -> dict[str, Radio]:
        return {r.serial: r for r in radios}

    return SimpleNamespace(sdr_radios=sdr_radios)


def _scan(*serials: str) -> dict[str, Any]:
    return {"sysfs_readable": True, "sdrs": [{"serial": s} for s in serials]}


async def _ask(usb: dict[str, Any] | None, store: Any, **kw: Any) -> Any:
    return await resolve.for_purpose(_client(usb), "t", store, CTX, GENERAL, None, **kw)


async def test_naming_a_radio_asks_whether_THAT_one_may_have_the_job() -> None:
    # Serial order would give the whip; the owner tapped the wire.
    got = await _ask(
        _scan(WHIP, WIRE), _store(Radio(WHIP, name="Desk whip"), Radio(WIRE, name="Long wire"))
    )

    assert got.serial == WHIP  # ...with no serial named

    got = await _ask(
        _scan(WHIP, WIRE),
        _store(Radio(WHIP, name="Desk whip"), Radio(WIRE, name="Long wire")),
        serial=WIRE,
    )

    assert got.serial == WIRE
    assert resolve.refusal(got) is None


async def test_a_reserved_radio_is_a_refusal_the_owner_has_to_act_on() -> None:
    # `reserved` had to be added to OWNER_MUST_ACT: without it the refusal sentence was
    # composed and then thrown away, and the route started the job on the radio anyway.
    got = await _ask(_scan(WHIP), _store(Radio(WHIP, name="Desk whip", role="aprs")), serial=WHIP)

    assert got.serial is None
    assert resolve.refusal(got) is not None


async def test_a_named_radio_survives_a_scan_that_could_not_see() -> None:
    """The case where being wrong is invisible.

    With no scan there is no way to check whether the named radio is attached — but the
    owner named it, and passing it through is strictly better than the historical
    "whatever librtlsdr enumerates first": if it is gone the sidecar fails on a device it
    can prove is missing, instead of quietly opening the other antenna."""
    got = await _ask(None, _store(Radio(WHIP), Radio(WIRE)), serial=WIRE)

    assert got.serial == WIRE
    assert resolve.refusal(got) is None


async def test_no_scan_and_no_name_is_still_the_one_dongle_answer() -> None:
    got = await _ask(None, _store(Radio(WHIP)))

    assert got.serial is None
    assert got.reason == "unknown"
    # NOT a refusal: naming no radio is exactly what a one-dongle box always did.
    assert resolve.refusal(got) is None


@pytest.mark.parametrize("serial", [WIRE, ""])
async def test_an_absent_radio_is_never_silently_replaced(serial: str) -> None:
    got = await _ask(_scan(WHIP), _store(Radio(WHIP), Radio(WIRE, name="Long wire")), serial=serial)

    if serial:
        assert got.serial is None
        assert resolve.refusal(got) is not None
    else:
        # An empty serial is "no radio named", not "a radio called nothing".
        assert got.serial == WHIP
