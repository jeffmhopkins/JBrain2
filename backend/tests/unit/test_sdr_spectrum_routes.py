"""The three routes behind the live waterfall.

What is worth testing here is not "does it proxy" — it is the arithmetic and the
refusals, because both are things the owner meets as a picture that is wrong rather
than as an error. A one-hop band has to get the bin width its own capture produces, a
span too wide for one capture has to fall back to the tool that can hop, a refusal has
to arrive as a sentence, and moving the picture to another band must never release the
radio in between — that window is how a waterfall disappears because someone changed
band.
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
from jbrain.sdr.tuner import live_bin_hz

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


def test_a_one_hop_section_carries_the_bin_width_ITS_capture_will_produce() -> None:
    """Not a number anyone typed and not one rtl_power granted: `air-tower` is captured
    at 2.4 MS/s over 4000 bins, so a bin is 600 Hz EXACTLY. The pairing is chosen for
    that (`bands.LIVE_CAPTURES`) — at 4096 bins the same rate is 585.9375 Hz, and a
    frame rounding that to 586 is 256 Hz out at its top edge with nothing able to tell.

    The table says it; F6 is what puts it on the wire, and `_span` holds it back until
    then (see the test below). This is the row, not the request."""
    section = bands.by_id("air-tower")
    assert section is not None

    start, stop, bin_hz, capture = sdr_api._span("air-tower", None, None)

    assert (start, stop) == (section.start_hz, section.stop_hz)
    # F6 put it on the wire: the width IS the capture's, and the capture travels with it.
    assert capture == (2_400_000, 4_000, 1)
    assert bin_hz == 600
    assert (section.sample_rate_hz, section.fft_bins) == (2_400_000, 4_000)
    assert section.live_bin_hz == 600
    assert isinstance(section.live_bin_hz, int)  # exact, so `sameBand` can compare it


def test_the_fast_tier_never_asks_rtl_power_for_sub_kilohertz_bins() -> None:
    """**Transitional, and F6 deletes it with `SPECTRUM_ENGINE_IS_IQ`.**

    The table's `rate / N` is 250-600 Hz, and until the I/Q engine exists that number
    is handed to `rtl_power -f start:stop:bin`, which honours it: `air-tower` goes from
    512 bins a frame to 4096, and `csv_dbm` prints `i1..i2` INCLUSIVE and then repeats
    `avg[i2]`, so the block is 4097 columns. That is ~29 kB per frame instead of ~3.6,
    relayed on the api's own event loop and rounded value by value in pure Python, for
    a picture no finer than the `%.2f` bin width the tool prints — 488.28 read back as
    488, putting a 4096-bin frame ~1150 Hz out at the top. All cost, no benefit, on the
    engine the width was not computed for.

    Asserted as a floor rather than as an exact number so it survives a re-tabling: no
    fast section may ask the running engine for a bin finer than a kilohertz."""
    if sdr_api.SPECTRUM_ENGINE_IS_IQ:  # pragma: no cover - F6 deletes this whole test
        pytest.skip("the I/Q engine computes the width itself; the ladder is gone")

    fast = [s for s in bands.SECTIONS if s.live == bands.LIVE_FAST]
    assert fast, "the table lost its one-hop sections"
    for section in fast:
        _start, _stop, bin_hz, _capture = sdr_api._span(section.id, None, None)

        assert bin_hz == live_bin_hz(section.span_hz, section.sweep_bin_hz), section.id
        assert bin_hz >= 1_000, section.id
        # And the width the tool would really emit, which is what crosses the wire.
        assert (section.span_hz // bin_hz) + 1 <= bands.LIVE_MAX_BINS, section.id


def test_a_multi_hop_section_still_gets_rtl_powers_own_ladder() -> None:
    """20 MHz of FM broadcast is more than one capture, so it is still swept — hops, one
    row a second, and a bin width the tool picks. Keeping that here is the point of
    splitting the two tiers rather than pretending one engine serves both."""
    section = bands.by_id("fm-broadcast")
    assert section is not None
    assert section.sample_rate_hz == 0

    _start, _stop, bin_hz, capture = sdr_api._span("fm-broadcast", None, None)

    # F11: 20 MHz is 11 hops on OUR engine now, at 9375 Hz bins where the tool gave
    # 19531 — so the capture IS named, with the hop count beside it.
    assert capture == (2_400_000, 256, 11)
    assert bin_hz == 9375


def test_an_explicit_range_is_the_section_it_names_in_numbers() -> None:
    """The expert path and the band button must produce the SAME picture, or the width
    of a bin depends on how the owner asked for the band.

    Covered on the rows where the two paths USED to disagree, which is the whole point:
    a derived answer takes the smallest capture that covers the range, while a curated
    row may deliberately name a larger one — `mw` is sampled at 2.048 MS/s so that
    `R/2 <= fc` holds and the picture does not fold, where the derived answer is 1.6.
    On the slow tier the same split ran the other way: a hand-typed 144.0-148.0 got the
    25 kHz default while the `2m-all` button got the 5 kHz the row asks for."""
    for section_id, (start_mhz, stop_mhz) in {
        "mw": (0.53, 1.70),
        "murs": (151.8, 154.65),
        "2m-all": (144.0, 148.0),
        "2m-ssb": (144.1, 144.3),
    }.items():
        section = bands.by_id(section_id)
        assert section is not None

        typed = sdr_api._span(None, start_mhz, stop_mhz)
        pressed = sdr_api._span(section_id, None, None)

        assert typed == pressed, section_id


def test_a_range_that_is_no_section_still_gets_a_derived_answer() -> None:
    """The lookup is EXACT, not nearest. 430-435 MHz is nobody's curated row, so it
    takes the default rather than inheriting settings chosen for a band it only
    overlaps — a rate picked for 2 MHz would draw a 5 MHz span's edges inside the IF
    rolloff, which reads as a dead band edge."""
    assert bands.by_edges(430_000_000, 435_000_000) is None

    _start, _stop, bin_hz, capture = sdr_api._span(None, 430.0, 435.0)

    # F11: 5 MHz is three hops on our own engine, so the derived answer is now the
    # capture's own width rather than rtl_power's ladder.
    assert capture == (2_400_000, 1_024, 3)
    assert bin_hz == 2343.75


def test_a_hand_entered_range_too_wide_for_one_capture_is_hopped() -> None:
    # 4 MHz is two hops however the owner asked for it, so this is rtl_power's ladder
    # in both engines — and the range is `2m-all`'s, so it is that row's width.
    whole = bands.by_id("2m-all")
    assert whole is not None

    _start, _stop, bin_hz, capture = sdr_api._span(None, 144.0, 148.0)

    # F11: the same three hops the band button gets, because both ask the same question
    # of the same row — which is what the equality test above this one is for.
    assert capture == (2_400_000, 1_024, 3)
    assert bin_hz == 2343.75


def test_shortwave_is_drawn_rather_than_refused() -> None:
    """F8. It used to come back "a sweep cannot go below 24 MHz" — true of rtl_power and
    of nothing else. The live engine reads raw I/Q and sets direct sampling mode 2, so
    40 m is a picture now, at the width its own capture makes."""
    forty = bands.by_id("40m")
    assert forty is not None

    start, stop, bin_hz, capture = sdr_api._span("40m", None, None)

    assert (start, stop) == (7_125_000, 7_300_000)
    assert forty.live_bin_hz == 250
    # F6 transforms it: 256 kS/s over 1024 bins, and the sidecar is told exactly that.
    assert (bin_hz, capture) == (250, (256_000, 1_024, 1))


def test_shortwave_wider_than_one_capture_is_refused_in_words() -> None:
    """The one surface an owner with no terminal has (CLAUDE.md #10). The refusal down
    here is narrower than it was, and it has to say which limit it hit."""
    with pytest.raises(HTTPException) as raised:
        sdr_api._span(None, 3.0, 8.0)

    assert raised.value.status_code == 400
    assert "more than one capture" in str(raised.value.detail)


def test_a_span_wider_than_the_radio_can_sweep_says_so() -> None:
    with pytest.raises(HTTPException) as raised:
        sdr_api._span(None, 400.0, 500.0)

    assert raised.value.status_code == 400
    assert "Pick a section" in str(raised.value.detail)


def test_a_section_that_does_not_exist_is_a_404() -> None:
    with pytest.raises(HTTPException) as raised:
        sdr_api._span("no-such-band", None, None)

    assert raised.value.status_code == 404


def test_a_waterfall_with_no_range_at_all_is_refused() -> None:
    with pytest.raises(HTTPException) as raised:
        sdr_api._span(None, None, None)

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


async def test_a_slow_reset_is_never_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEASURED 2026-09-04, and the reason this is a test rather than a comment: a
    `USBDEVFS_RESET` outran the timeout and the owner got a 500 with a traceback for an
    operation that had in fact HAPPENED — the device left the bus. Answering "the radio
    did not reset" would have been worse than the traceback, because it is false. A
    timeout licenses "look again" and nothing more."""

    class _Slow:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        async def post(self, _path: str, **_kw: Any) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

    monkeypatch.setattr(sdr_api.httpx, "AsyncClient", lambda **_kw: _Slow())

    with pytest.raises(HTTPException) as raised:
        await sdr_api._post(_settings(), "/reset", {})

    assert raised.value.status_code == 504
    said = str(raised.value.detail)
    assert "may still be happening" in said
    # The words that would make it a lie.
    assert "did not reset" not in said
    assert "failed" not in said.lower()


async def test_a_reset_gets_longer_than_an_ordinary_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The kernel waits on a port that may never answer, and that wait IS the operation.
    seen: list[float] = []

    class _Timed:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        async def post(self, _path: str, **_kw: Any) -> httpx.Response:
            return httpx.Response(
                200, json={"reset": True}, request=httpx.Request("POST", "http://sdr/reset")
            )

    def client(**kw: Any) -> Any:
        seen.append(kw["timeout"])
        return _Timed()

    monkeypatch.setattr(sdr_api.httpx, "AsyncClient", client)

    await sdr_api._post(_settings(), "/listen/start", {})
    await sdr_api._post(_settings(), "/reset", {}, wait_s=sdr_api.RESET_TIMEOUT_S)

    assert seen[1] > seen[0]


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


def test_a_one_hop_range_sends_the_capture_to_the_sidecar() -> None:
    """The engine choice travels as the PRESENCE of these two fields. There is no flag
    on the wire and no third state, so the two sides cannot fall out of step about which
    engine is drawing — which is how a `rate / N` width once reached rtl_power and came
    back 4097 columns wide (§6.4)."""
    assert sdr_api._capture_body((2_400_000, 4_000, 1)) == {
        "rate_hz": 2_400_000,
        "bins": 4_000,
        "hops": 1,
    }


def test_a_multi_hop_range_sends_no_capture_fields_at_all() -> None:
    """Absent rather than null: a sidecar that predates F6 then sees exactly the body it
    saw before, and hops the range with rtl_power as it always did."""
    assert sdr_api._capture_body(None) == {}


def test_the_width_and_the_engine_are_one_decision() -> None:
    """The invariant that makes the wire contract safe. An exact `rate / N` width is
    only ever sent WITH the capture that produces it, and rtl_power's ladder width is
    only ever sent without one. Asserted across the whole table rather than on a row,
    because the failure it guards is a single row drifting."""
    for section in bands.SECTIONS:
        _start, _stop, bin_hz, capture = sdr_api._span(section.id, None, None)

        if capture is None:
            continue
        rate_hz, fft_bins, _hops = capture
        assert bin_hz == bands.bin_width_hz(rate_hz, fft_bins), section.id


# --- F11: wide bands hop on the I/Q engine rather than falling to rtl_power ---------


def test_the_two_bands_that_needed_a_tool_now_hop_on_our_own_engine() -> None:
    """`rtl_power`'s one row a second is not the cost of hopping — it is
    `if (interval < 1) interval = 1;` in its own C. The retune works on a live stream
    (F0), so a wide span is several captures stitched, and the bins come out FINER than
    the tool gave: 9,375 Hz across the FM dial where rtl_power gave 19,531."""
    rate_hz, bins, hops = bands.hop_plan(88_000_000, 108_000_000)  # type: ignore[misc]

    assert (rate_hz, bins, hops) == (2_400_000, 256, 11)
    assert bands.bin_width_hz(rate_hz, bins) == 9375
    # And it fits the frame budget it was planned against.
    assert bands.hop_usable_bins(bins) * hops <= bands.LIVE_MAX_BINS


def test_a_span_one_capture_covers_is_never_hopped() -> None:
    """Hopping costs a retune per hop and measures each bin once a sweep. Where one
    tuning covers the range, doing it in several is strictly worse."""
    assert bands.capture_for(144_100_000, 144_300_000) is not None
    assert bands.hop_plan(144_100_000, 144_300_000) is None


def test_wide_shortwave_is_still_refused_rather_than_folded() -> None:
    """Below 24 MHz the ADC is fed directly and each hop's centre would have to satisfy
    the Nyquist window on its own; a plan that ignored that would draw a picture made
    of folded images and look perfectly plausible."""
    assert bands.hop_plan(3_000_000, 8_000_000) is None


def test_a_hopped_span_sends_its_plan_to_the_sidecar(mocker: Any = None) -> None:
    """The engine choice and the hop count travel together. `hops` is sent always, not
    only when interesting: a sidecar reading it as absent would sweep 20 MHz at one
    tuning and draw the wrong band confidently."""
    _start, _stop, bin_hz, capture = sdr_api._span("fm-broadcast", None, None)

    assert capture == (2_400_000, 256, 11)
    assert bin_hz == 9375
    assert sdr_api._capture_body(capture) == {
        "rate_hz": 2_400_000,
        "bins": 256,
        "hops": 11,
    }


def test_every_hopped_section_still_declares_the_width_it_will_produce() -> None:
    """The invariant that survives F11: an exact `rate / bins` width is only ever sent
    with the capture that produces it, hopped or not."""
    for section in bands.SECTIONS:
        _start, _stop, bin_hz, capture = sdr_api._span(section.id, None, None)

        if capture is None:
            continue
        rate_hz, fft_bins, _hops = capture
        assert bin_hz == bands.bin_width_hz(rate_hz, fft_bins), section.id
