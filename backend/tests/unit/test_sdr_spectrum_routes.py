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
from typing import Any, get_type_hints

import httpx
import pytest
from fastapi import HTTPException

from jbrain.api import sdr as sdr_api
from jbrain.sdr import bands
from jbrain.sdr.roles import Radio

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


def test_every_curated_section_has_a_capture_plan() -> None:
    """B1 removed the second engine, so this is now a completeness requirement rather
    than a comparison between two ladders: a section with no plan would be a button in
    the band sheet that answers 400.

    Checked over the whole table rather than a sample, because the table is the thing
    that can grow a row nobody plans for."""
    for section in bands.SECTIONS:
        _start, _stop, bin_hz, capture = sdr_api._span(section.id, None, None)

        assert capture is not None, section.id
        rate_hz, fft_bins, hops = capture
        assert bin_hz == bands.bin_width_hz(rate_hz, fft_bins), section.id
        assert hops >= 1, section.id


def test_a_multi_hop_section_is_stitched_rather_than_swept() -> None:
    """20 MHz of FM broadcast is more than one capture — and since F11 that is several
    captures stitched on OUR engine rather than a job for a tool with a one-row-a-second
    clamp and a coarser bin."""
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


def _posts(
    monkeypatch: pytest.MonkeyPatch, stored: Radio | None = None
) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    radio = stored or Radio(serial="77192819")

    async def post(_settings: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
        seen.append((path, body))
        return {"session_id": "s1", "purpose": "spectrum"}

    async def radio_for(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(serial="77192819", radio=radio, conflict=None, refusal=None)

    class _Store:
        async def sdr_radios(self, _ctx: Any) -> dict[str, Radio]:
            return {radio.serial: radio}

    async def session_radio(*_a: Any, **_k: Any) -> str:
        return radio.serial

    monkeypatch.setattr(sdr_api, "_post", post)
    monkeypatch.setattr(sdr_api, "_radio_for", radio_for)
    monkeypatch.setattr(sdr_api, "_refuse", lambda _c: None)
    monkeypatch.setattr(sdr_api, "get_settings_store", lambda _r: _Store())
    monkeypatch.setattr(sdr_api, "ctx_for", lambda _o: object())
    # Which dongle holds the session being retuned. Read off the sidecar's health in
    # production; here the fake answers with the one radio these tests have.
    monkeypatch.setattr(sdr_api, "_session_radio", session_radio)
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
        _request(),
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


# --- filter bandwidth -------------------------------------------------------------


async def test_a_bandwidth_is_forwarded_and_absence_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitted rather than sent as null when the caller says nothing.

    A `"bandwidth_hz": None` on the wire would make this route the place that decides
    the default, and it is not: the ladder and its widest rung live in the demodulator,
    and a second opinion here is a second thing to keep in step."""
    seen = _posts(monkeypatch)

    await sdr_api.listen(_request(), _settings(), OWNER, frequency_mhz=5.0, mode="am")
    _path, body = seen[-1]
    assert "bandwidth_hz" not in body

    await sdr_api.listen(
        _request(), _settings(), OWNER, frequency_mhz=5.0, mode="am", bandwidth_hz=6_000
    )
    _path, body = seen[-1]
    assert body["bandwidth_hz"] == 6_000


async def test_retuning_can_change_only_the_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """How the control sends a new width: the same frequency back with a new bandwidth.

    The session keeps its width across a retune, so this is the whole mechanism — there
    is no separate "set bandwidth" route to get out of step with `/tune`."""
    seen = _posts(monkeypatch)

    await sdr_api.tune(
        _request(), _settings(), OWNER, frequency_mhz=5.0, session_id="s1", bandwidth_hz=4_000
    )

    path, body = seen[-1]
    assert path == "/listen/tune"
    assert body == {
        "frequency_hz": 5_000_000,
        "session_id": "s1",
        "bandwidth_hz": 4_000,
    }
    # No mode: sending one would reset the width to that mode's default on the sidecar,
    # which is exactly what a filter-only change must not do.
    assert "mode" not in body


@pytest.mark.parametrize("route", [sdr_api.tune, sdr_api.listen])
def test_the_bandwidth_is_bounded_by_the_schema(route: Any) -> None:
    """Bounded so nonsense is a 422 here rather than a 400 from a round trip.

    This pins the BOUND, not a request: the sidecar stays the authority on which exact
    widths are real, and these limits only refuse values no ladder could ever hold. Both
    routes are checked because they are separate signatures that must not drift.

    Read through `get_type_hints` because `from __future__ import annotations` leaves
    every annotation in this module a string — reading `__annotations__` directly finds
    the source text and asserts nothing."""
    query = get_type_hints(route, include_extras=True)["bandwidth_hz"].__metadata__[0]
    # FastAPI keeps the constraints as annotated-types markers on the Query rather than
    # as attributes of it, so `query.ge` does not exist and reading it would only raise.
    limits = {type(m).__name__: m for m in query.metadata}
    low, high = limits["Ge"].ge, limits["Le"].le

    assert (low, high) == (sdr_api.MIN_BANDWIDTH_HZ, sdr_api.MAX_BANDWIDTH_HZ)
    # Wide enough for every real rung — the narrowest is SSB's 1.8 kHz and the widest is
    # wide FM's 180 kHz — and tight enough to catch the likely units mistake, which is
    # kHz sent as Hz or Hz sent as kHz.
    assert low <= 1_800 and high >= 180_000
    for absurd in (0, 6, 500, 500_000):
        assert not (low <= absurd <= high), absurd


# --- what the radio's own settings do to a start ------------------------------------
#
# Gain and an upconverter are stored per radio, on the same `sdr_radios` entry as the
# name and the role (docs/mocks/radio-settings/README.md). What the api owes them is to
# read them off the radio it just chose and put them on the body — every purpose, with
# the frequency left alone.

CONVERTED = Radio(serial="77192819", gain="20", upconverter_hz=125_000_000)


async def test_the_radios_stored_gain_and_offset_reach_a_waterfall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _posts(monkeypatch, CONVERTED)

    await sdr_api.spectrum_start(_request(), _settings(), OWNER, section="fm-broadcast")

    _path, body = seen[0]
    assert body["gain"] == "20"
    assert body["upconverter_hz"] == 125_000_000
    # The EDGES are the owner's, not the tune. They label the axis.
    assert body["start_hz"] == 88_000_000


async def test_a_gain_asked_for_by_this_call_beats_the_radios_standing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ONE mechanism, not two. The per-session `?gain=` predates the setting and is how
    the debug console and jerv measure the same band at two gains on purpose, so an
    explicit request is not a default to be overridden."""
    seen = _posts(monkeypatch, CONVERTED)

    await sdr_api.spectrum_start(_request(), _settings(), OWNER, section="fm-broadcast", gain="0")

    assert seen[0][1]["gain"] == "0"


async def test_an_unconfigured_radio_sends_what_it_always_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A radio nobody has opened this screen for must behave bit-for-bit as it did: no
    gain named, so the sidecar's per-purpose default applies, and no offset."""
    seen = _posts(monkeypatch)

    await sdr_api.spectrum_start(_request(), _settings(), OWNER, section="fm-broadcast")
    await sdr_api.listen(_request(), _settings(), OWNER, frequency_mhz=146.52)

    for _path, body in seen:
        assert body["gain"] is None
        assert body["upconverter_hz"] == 0


async def test_the_frequency_on_the_wire_is_never_the_shifted_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this feature is most likely to cause, guarded at the api door as
    well as in the sidecar: 7.200 MHz through a 125 MHz converter is a request for
    7.200, plus an offset the RADIO applies. A `frequency_hz` of 132_200_000 here would
    label a session, its recording and its heard log 125 MHz wrong."""
    seen = _posts(monkeypatch, CONVERTED)

    await sdr_api.listen(_request(), _settings(), OWNER, frequency_mhz=7.2, mode="usb")

    _path, body = seen[0]
    assert body["frequency_hz"] == 7_200_000
    assert body["upconverter_hz"] == 125_000_000
    assert "132200000" not in json.dumps(body)
