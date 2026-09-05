"""The F0 probe's route: the lease, the bounds, and the sentence a refusal arrives as.

The verdict itself is the sidecar's — `deploy/sdr/radio.py`'s `probe`, covered against
a fake device in `supervisor/tests/test_sdr_radio.py`. What this side owns is which
radio the probe is aimed at, which frequencies it may be aimed at, and how long it is
allowed to take. All three are things the owner meets as a wrong answer rather than as
an error, which is why they are tested here rather than assumed
(docs/plans/SDR_IQ_SPECTRUM_PLAN.md §7, F0).
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from jbrain.api import debug

WHIP, WIRE = "09022796", "77192819"


class _Settings:
    sdr_url = "http://sdr:8000"
    supervisor_token = "t"


def _scan(*serials: str) -> dict[str, Any]:
    return {
        "sysfs_readable": True,
        "devices": [],
        "sdrs": [
            {
                "name": f"1-{i}",
                "usb_id": "0bda:2838",
                "product": "NESDR SMArt v5",
                "serial": serial,
                "device_node": f"/dev/bus/usb/001/{10 + i:03d}",
                "drivers": [],
            }
            for i, serial in enumerate(serials)
        ],
    }


def _request(scan: dict[str, Any] | None = None) -> Request:
    class _Client:
        async def get(self, _path: str, **_kw: Any) -> httpx.Response:
            return httpx.Response(
                200,
                json=scan if scan is not None else _scan(WHIP, WIRE),
                request=httpx.Request("GET", "http://supervisor/usb"),
            )

    app = FastAPI()
    app.state.supervisor_client = _Client()
    return Request({"type": "http", "app": app, "headers": [], "method": "POST", "path": "/"})


def _sidecar(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> list[dict[str, Any]]:
    """Point the route's httpx client at a fake sidecar, recording what it was sent."""
    sent: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, **kw: Any) -> None:
            sent.append({"timeout": kw.get("timeout")})

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def post(self, path: str, json: Any = None) -> httpx.Response:  # noqa: A002
            sent[-1].update(path=path, body=json)
            return response

    monkeypatch.setattr(debug.httpx, "AsyncClient", _FakeClient)
    return sent


def _verdict(**over: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={"ok": True, "summary": "every claim held", **over},
        request=httpx.Request("POST", "http://sdr:8000/soapy/probe"),
    )


async def _probe(request: Request, **kwargs: Any) -> dict[str, Any]:
    return await debug.sdr_soapy_probe(request, cast(Any, _Settings()), cast(Any, None), **kwargs)


async def test_the_named_radio_is_the_one_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the probe on a two-dongle box: `serial=` is the claim under test, so
    the route must be able to aim at either radio by name."""
    sent = _sidecar(monkeypatch, _verdict())

    out = await _probe(_request(), serial=WIRE, frequency_mhz=10.0)

    assert out["ok"] is True
    assert sent[0]["path"] == "/soapy/probe"
    assert sent[0]["body"] == {
        "serial": WIRE,
        "center_hz": 10_000_000,
        "rate_hz": 256_000,
        "bins": 1024,
    }


async def test_it_defaults_to_wwv(monkeypatch: pytest.MonkeyPatch) -> None:
    """10 MHz through the direct-sampling branch, at 256 kS/s over 1024 bins — 250 Hz
    exactly. A carrier well clear of the frame's own floor there is the whole HF path
    working end to end, on a frequency that is a fact rather than a guess."""
    sent = _sidecar(monkeypatch, _verdict())
    monkeypatch.setattr(debug, "_radio", _resolver(WHIP))

    await _probe(_request())

    assert sent[0]["body"]["center_hz"] == 10_000_000
    assert sent[0]["body"]["serial"] == WHIP


def _resolver(serial: str | None):
    async def _pick(*_a: Any, **_k: Any) -> str | None:
        return serial

    return _pick


async def test_with_no_serial_it_asks_the_same_resolver_every_other_door_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console is a THIRD door onto the radio. A probe that picked a dongle for
    itself could take the one the owner reserved for APRS."""
    sent = _sidecar(monkeypatch, _verdict())
    monkeypatch.setattr(debug, "_radio", _resolver(WIRE))

    await _probe(_request())

    assert sent[0]["body"]["serial"] == WIRE


async def test_a_serial_that_names_no_radio_is_a_404_not_a_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise a typo reaches the sidecar and returns as "could not open the radio",
    which reads like a broken dongle rather than a mistyped serial."""
    _sidecar(monkeypatch, _verdict())

    with pytest.raises(HTTPException) as raised:
        await _probe(_request(_scan(WHIP)), serial=WIRE)

    assert raised.value.status_code == 404
    assert WIRE in str(raised.value.detail)


async def test_a_frequency_in_the_hole_is_refused_with_the_one_it_would_deliver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """14.4-24 MHz is inside the radio's range and reachable by neither path: below
    24 MHz it samples directly, and that path is honest only to 14.4. A probe at 18.1
    would measure 10.7 and report a confident peak in the wrong band — which is exactly
    the class of lie this whole wave exists to prevent."""
    sent = _sidecar(monkeypatch, _verdict())

    with pytest.raises(HTTPException) as raised:
        await _probe(_request(), serial=WHIP, frequency_mhz=18.1)

    assert raised.value.status_code == 400
    assert "10.7" in str(raised.value.detail)
    assert sent == [], "the radio must not be taken for a request that cannot be honest"


async def test_it_waits_longer_than_an_ordinary_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It opens a device, times a dozen USB buffers, retunes four times and then starves
    the stream on purpose. The 30 s default assumes a call that either answers or has
    already failed."""
    sent = _sidecar(monkeypatch, _verdict())

    await _probe(_request(), serial=WHIP)

    assert sent[0]["timeout"] == debug.SOAPY_PROBE_TIMEOUT_S
    assert debug.SOAPY_PROBE_TIMEOUT_S > 30.0


async def test_the_sidecars_refusal_arrives_as_its_own_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 here means another job holds that dongle, and the sidecar's own words name
    which job — "the radio is busy" is not something an owner can act on."""
    _sidecar(
        monkeypatch,
        httpx.Response(
            409,
            json={"detail": "the radio (77192819) is already logging APRS"},
            request=httpx.Request("POST", "http://sdr:8000/soapy/probe"),
        ),
    )

    with pytest.raises(HTTPException) as raised:
        await _probe(_request(), serial=WIRE)

    assert raised.value.status_code == 409
    assert "logging APRS" in str(raised.value.detail)
