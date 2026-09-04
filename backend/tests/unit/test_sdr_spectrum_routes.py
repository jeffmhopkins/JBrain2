"""The three routes behind the live waterfall.

What is worth testing here is not "does it proxy" — it is the arithmetic and the
refusals, because both are things the owner meets as a picture that is wrong rather
than as an error. A span too fine to send has to be COARSENED rather than refused, a
band that cannot be swept has to say so in a sentence, and moving the picture to
another band must never release the radio in between — that window is how a waterfall
disappears because someone changed band.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from jbrain.api import sdr as sdr_api
from jbrain.sdr import bands
from jbrain.sdr.roles import Radio
from jbrain.sdr.tuner import SPECTRUM_MAX_BINS

# Typed `Any` so the fakes can stand in for `SettingsDep`/`OwnerDep` without a
# `type: ignore` on every call — the routes read two attributes off each.
OWNER: Any = SimpleNamespace(id="owner", kind="owner")


class _Settings:
    sdr_url = "http://sdr:8000"
    supervisor_token = "t"


def _settings() -> Any:
    return _Settings()


def _request(disconnected: bool = False) -> Any:
    async def is_disconnected() -> bool:
        return disconnected

    return SimpleNamespace(
        is_disconnected=is_disconnected,
        app=SimpleNamespace(state=SimpleNamespace()),
    )


# --- the range ------------------------------------------------------------------


def test_a_named_section_brings_its_own_bin_width() -> None:
    """The width someone chose while reading a band plan beats anything this route
    could compute from the span alone (`bands.sweep_bin_hz`)."""
    section = bands.by_id("fm-broadcast")
    assert section is not None

    start, stop, bin_hz = sdr_api._span("fm-broadcast", None, None, None)

    assert (start, stop) == (section.start_hz, section.stop_hz)
    assert bin_hz == section.sweep_bin_hz


def test_an_explicit_range_is_the_expert_way_in() -> None:
    start, stop, bin_hz = sdr_api._span(None, 144.0, 148.0, 5_000)

    assert (start, stop, bin_hz) == (144_000_000, 148_000_000, 5_000)


def test_a_range_too_fine_to_send_is_coarsened_not_refused() -> None:
    """A live view is where the owner asks for a whole band at once. "Too wide" is a
    worse answer than a coarser picture they can zoom into — and the row carries its
    own bin width, so nothing downstream has to be told."""
    _start, _stop, bin_hz = sdr_api._span(None, 144.0, 148.0, 100)

    assert 4_000_000 // bin_hz <= SPECTRUM_MAX_BINS
    assert bin_hz == 1_600  # 100 Hz doubled until the frame fits


def test_shortwave_is_refused_in_words_not_a_validation_blob() -> None:
    """The one surface an owner with no terminal has (CLAUDE.md #10). HF listens
    perfectly and cannot be swept, and that difference has to be said."""
    with pytest.raises(HTTPException) as raised:
        sdr_api._span(None, 7.0, 7.3, None)

    assert raised.value.status_code == 400
    assert "cannot go below" in str(raised.value.detail)
    assert "still listen" in str(raised.value.detail)


def test_a_span_wider_than_the_radio_can_sweep_says_so() -> None:
    with pytest.raises(HTTPException) as raised:
        sdr_api._span(None, 400.0, 500.0, None)

    assert raised.value.status_code == 400
    assert "Pick a section" in str(raised.value.detail)


def test_a_section_that_does_not_exist_is_a_404() -> None:
    with pytest.raises(HTTPException) as raised:
        sdr_api._span("no-such-band", None, None, None)

    assert raised.value.status_code == 404


def test_a_waterfall_with_no_range_at_all_is_refused() -> None:
    with pytest.raises(HTTPException) as raised:
        sdr_api._span(None, None, None, None)

    assert raised.value.status_code == 400
    assert "band section" in str(raised.value.detail)


# --- starting and moving --------------------------------------------------------


def _posts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def post(_settings: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
        seen.append((path, body))
        return {"session_id": "s1", "purpose": "spectrum"}

    async def radio_for(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(
            serial="77192819", radio=Radio(serial="77192819"), conflict=None, refusal=None
        )

    monkeypatch.setattr(sdr_api, "_post", post)
    monkeypatch.setattr(sdr_api, "_radio_for", radio_for)
    monkeypatch.setattr(sdr_api, "_refuse", lambda _c: None)
    return seen


async def test_starting_names_the_radio_and_the_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _posts(monkeypatch)

    await sdr_api.spectrum_start(
        _request(),
        _settings(),
        OWNER,
        section="fm-broadcast",
    )

    path, body = seen[0]
    assert path == "/listen/start"
    assert body["purpose"] == "spectrum"
    assert body["serial"] == "77192819"
    assert body["start_hz"] == 88_000_000
    # No `frequency_hz` is sent at all: a span's centre is the only frequency it has,
    # and the sidecar is the one place that derives it.
    assert "frequency_hz" not in body


async def test_moving_the_picture_never_releases_the_radio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window this route exists to close: stop-then-start hands the dongle to
    whatever asks next, and the owner's waterfall vanishes because they changed band."""
    seen = _posts(monkeypatch)

    await sdr_api.spectrum_tune(
        _settings(),
        OWNER,
        section="air-tower",
        session_id="s1",
    )

    assert [path for path, _body in seen] == ["/listen/tune"]
    assert seen[0][1]["session_id"] == "s1"


# --- the stream -----------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, lines: list[str] | None = None, body: bytes = b"") -> None:
        self.status_code = status
        self._lines = lines or []
        self._body = body

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body


class _Stream:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    async def __aenter__(self) -> _Resp:
        return self._resp

    async def __aexit__(self, *_a: Any) -> bool:
        return False


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def stream(self, _method: str, _path: str) -> _Stream:
        return _Stream(self._resp)

    async def aclose(self) -> None:
        return None


def _upstream(monkeypatch: pytest.MonkeyPatch, resp: _Resp) -> None:
    monkeypatch.setattr(sdr_api.httpx, "AsyncClient", lambda **_kw: _Client(resp))


async def _collect(resp_out: Any) -> list[str]:
    return [chunk async for chunk in resp_out.body_iterator]


async def test_a_row_is_relayed_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """This route understands nothing about the picture, and that is the design: each
    row already says which band it covers, so a retune lands with no message here."""
    row = json.dumps({"start_hz": 88_000_000, "bin_hz": 25_000, "db": [-70.0]})
    _upstream(monkeypatch, _Resp(200, [row, "", '{"keepalive":true}']))

    out = await sdr_api.spectrum(_request(), _settings(), OWNER)

    assert await _collect(out) == [f"data: {row}\n\n", 'data: {"keepalive":true}\n\n']


async def test_the_sidecars_own_refusal_is_not_buried_in_a_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sidecar's 400s are sentences for an OPERATOR, not gateway faults.

    MEASURED on the box 2026-09-04: a dongle whose USB descriptors had stopped answering
    made rtl_power exit on `No matching devices found`, and the one thing the owner could
    act on would have reached them as a 502 reading "sdr sidecar: ..." — a status that
    says the box is broken, over a message that says which radio to reseat."""

    class _Posting:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        async def post(self, _path: str, **_kw: Any) -> httpx.Response:
            return httpx.Response(
                400,
                json={"detail": "the radio did not start: No matching devices found."},
                request=httpx.Request("POST", "http://sdr/listen/start"),
            )

    monkeypatch.setattr(sdr_api.httpx, "AsyncClient", lambda **_kw: _Posting())

    with pytest.raises(HTTPException) as raised:
        await sdr_api._post(_settings(), "/listen/start", {})

    assert raised.value.status_code == 400
    assert "No matching devices found" in str(raised.value.detail)
    assert "sdr sidecar" not in str(raised.value.detail)


async def test_a_busy_radio_reaches_the_owner_as_a_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that just ends is a waterfall that never paints, with nothing on screen
    to say why. The sidecar's own words are what names the job holding the radio."""
    refusal = json.dumps({"detail": "the radio is logging APRS, not watching the spectrum"})
    _upstream(monkeypatch, _Resp(409, body=refusal.encode()))

    out = await sdr_api.spectrum(_request(), _settings(), OWNER)

    assert await _collect(out) == [
        'data: {"error": "the radio is logging APRS, not watching the spectrum"}\n\n'
    ]


async def test_a_viewer_who_left_stops_the_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    # Otherwise a closed tab keeps a radio's rows flowing through this process for the
    # life of the session.
    row = json.dumps({"start_hz": 88_000_000, "bin_hz": 25_000, "db": [-70.0]})
    _upstream(monkeypatch, _Resp(200, [row, row]))

    out = await sdr_api.spectrum(_request(disconnected=True), _settings(), OWNER)

    assert await _collect(out) == []
