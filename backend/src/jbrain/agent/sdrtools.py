"""jerv's radio tools: take a radio, and hand it back.

`sdr_listen` is what makes the omnibox radio icon appear. That is not incidental —
the icon exists only while a session holds the tuner (the icon IS the lease,
docs/plans/SDR_RADIO_PLAN.md D7), so a tool that takes the lease is the only thing
that can put the control surface in front of the owner. Without it the tuner sheet
is unreachable: nothing else on the box starts a session.

The lease is PER RADIO and the box may have several, so `resolve.for_purpose` picks
which one before anything is taken, and a refusal names that radio and the job holding
it — `sdr_listen` returns it plainly rather than waiting. Telling the model which radio
is held lets it say so; queueing behind an unknown wait would just look like a hang.

Frequency and mode only — never a URL, and bounded here as well as in the api and
the sidecar. The `stream.py` SSRF guard is untouched by this path (§4.4).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.sdr import bands
from jbrain.sdr.health import session_for
from jbrain.sdr.resolve import refusal
from jbrain.sdr.roles import GENERAL, Choice
from jbrain.sdr.tuner import out_of_range

MODES = ("fm", "nfm", "wbfm", "am", "usb", "lsb")

# Broadcast FM is the common case a plain frequency implies, and getting it wrong is
# audible: narrowband on a broadcast station is mush. Anything at or above 88 and
# below 108 defaults to wide FM unless the caller says otherwise.
_BROADCAST_FM = (88.0, 108.0)

# What a purpose is called on the wire, and where APRS lives in North America when
# the owner does not say. 144.390 is the national channel — deliberately the default
# only because it is where traffic actually is; a private command frequency is an
# owner decision the plan holds open (APRS_CONTROL_PLAN.md §7).
PURPOSE_APRS = "aprs"
APRS_DEFAULT_MHZ = bands.APRS_HZ / 1_000_000


def _default_mode(mhz: float) -> str:
    return "wbfm" if _BROADCAST_FM[0] <= mhz < _BROADCAST_FM[1] else "fm"


#: How a tool asks which radio to open: `(session_ctx, purpose) -> Choice`. Injected
#: rather than built here because resolving needs the settings store and the supervisor
#: client, and these tools are constructed with neither.
RadioPicker = Callable[[SessionContext, str], Awaitable[Choice]]


#: The longest a signal measurement may hold the radio. Seconds, not minutes: the
#: comment in `listen.py` about an agent asking for an hour "because nothing in its
#: training says the radio is scarce" is about exactly this surface, and the sidecar's
#: own 900 s ceiling is far too generous to be the only guard. It also has to finish
#: inside `_call`'s timeout, since jerv has no way to wait on a job.
MAX_SIGNAL_SECONDS = 10.0
DEFAULT_SIGNAL_SECONDS = 3.0
#: A measurement is about one frequency, so the span is one capture wide rather than a
#: band: wide enough to show a carrier and its neighbours, narrow enough that the peak
#: means something about the frequency that was asked for.
SIGNAL_SPAN_HZ = 200_000
#: What a measurement asks for when no curated capture covers the span. Fine enough to
#: separate adjacent narrowband channels, coarse enough not to be noise.
SIGNAL_BIN_HZ = 250


def _bounded_seconds(raw: object) -> float:
    try:
        want = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_SIGNAL_SECONDS
    return max(1.0, min(want, MAX_SIGNAL_SECONDS))


def _signal_span(arguments: dict) -> tuple[int, int] | str:
    """The range to measure, from a section or a frequency. A sentence when neither."""
    named = arguments.get("section")
    if named:
        found = bands.by_id(str(named))
        if found is None:
            return f"No section called {named!r}. `sdr_read` with what=bands lists them."
        return found.start_hz, found.stop_hz
    try:
        mhz = float(arguments.get("frequency_mhz"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "Give me a frequency in MHz, or a section id from `sdr_read`."
    refused = out_of_range(mhz)
    if refused:
        return refused
    centre = int(round(mhz * 1_000_000))
    return centre - SIGNAL_SPAN_HZ // 2, centre + SIGNAL_SPAN_HZ // 2


def _signal_capture(start_hz: int, stop_hz: int) -> dict[str, int]:
    """The one-hop capture for this span, so the I/Q engine measures it rather than
    `rtl_power` — which reports its own dB scale, not dBFS, and is the very confusion
    F9 exists to stop."""
    capture = bands.capture_for(start_hz, stop_hz)
    if capture is None:
        return {}
    rate_hz, fft_bins = capture
    return {"rate_hz": rate_hz, "bins": fft_bins}


def _signal_bin_hz(start_hz: int, stop_hz: int) -> int | float:
    capture = bands.capture_for(start_hz, stop_hz)
    return bands.bin_width_hz(*capture) if capture is not None else SIGNAL_BIN_HZ


def _signal_reading(body: dict[str, Any], start_hz: int, stop_hz: int) -> str:
    """The measurement as a sentence about the MARGIN, not the raw level.

    An absolute dBFS figure means little here — no calibrated gain, about seven
    effective bits — so what carries information is how far the strongest bin stands
    over the frame's own floor. That is the same relative standard `sweep.steady` is
    calibrated on, where +6 dB found all 13 FM stations and nothing on a quiet band."""
    frame = body.get("frame") or {}
    span = f"{start_hz / 1e6:g}-{stop_hz / 1e6:g} MHz"
    if not body.get("frames"):
        return f"The radio gave me no measurement of {span} at all."
    floor = frame.get("floor_db")
    peak = frame.get("peak_db")
    if floor is None or peak is None:
        return (
            f"Nothing measurable across {span}: no noise floor at all, which is the "
            "antenna or the input rather than the band being quiet."
        )
    over = round(float(peak) - float(floor), 1)
    where = frame.get("peak_hz")
    at = f" at {float(where) / 1e6:.4f} MHz" if isinstance(where, int | float) else ""
    verdict = "something is transmitting" if over >= 6.0 else "nothing is standing out of the noise"
    return (
        f"Across {span}: {verdict}. Strongest bin{at} is {peak} dBFS, "
        f"{over} dB over a noise floor of {floor} dBFS."
    )


def _band_table() -> str:
    """Every section, one line each. No channels — that is what asking for one is for."""
    lines = [f"{len(bands.SECTIONS)} sections, region {bands.REGION}:"]
    for section in bands.SECTIONS:
        lines.append(
            f"- {section.id}: {section.name}, "
            f"{section.start_hz / 1e6:g}-{section.stop_hz / 1e6:g} MHz, "
            f"{section.mode}, live={section.live}" + (f" — {section.note}" if section.note else "")
        )
    return "\n".join(lines)


def _section_detail(section: bands.Section) -> str:
    lines = [
        f"{section.id}: {section.name} ({section.band})",
        f"{section.start_hz / 1e6:g}-{section.stop_hz / 1e6:g} MHz, mode {section.mode}",
        f"channel spacing {section.channel_hz or 0} Hz, tuning step {section.step_hz} Hz",
        f"live={section.live}"
        + (", continuous carriers" if section.continuous else ", intermittent traffic"),
    ]
    if section.note:
        lines.append(section.note)
    if section.channels:
        lines.append("Named channels:")
        lines.extend(
            f"- {c.hz / 1e6:g} MHz {c.name}" + (f" — {c.note}" if c.note else "")
            for c in section.channels
        )
    return "\n".join(lines)


def _radio_roster(answered: tuple[int, dict[str, Any]]) -> str:
    """Which dongles are attached and what each is doing, from the sidecar's own view."""
    status, body = answered
    if status != 200:
        return "I couldn't reach the radio service to ask what is attached."
    holding = body.get("holding")
    rows = holding if isinstance(holding, list) else []
    if not rows:
        return "No radio is doing anything right now."
    lines = ["Radios in use:"]
    for row in rows:
        if not isinstance(row, dict):
            continue
        serial = row.get("serial") or "(whichever enumerated first)"
        lines.append(f"- {serial}: {row.get('purpose') or 'in use'}")
    return "\n".join(lines)


def build_sdr_handlers(
    base_url: str, pick_radio: RadioPicker | None = None
) -> dict[str, ToolHandler]:
    """Bind the radio tools to the sidecar. Empty base_url => no radio => no tools,
    the same graceful degrade the image and transcription tools use.

    `pick_radio` is how these tools honour the owner's radio settings. Without it they
    take whichever radio librtlsdr enumerates first — which is the historical behaviour
    and correct on a one-dongle box, and is why it stays optional rather than required.
    On a two-dongle box it is the bug: this is the CONVERSATIONAL path to APRS logging
    (`APRS_CONTROL_PLAN.md` P1a), so an unwired picker means asking the assistant to
    start logging quietly opens the wrong antenna while the PWA switch beside it
    refuses to."""
    if not base_url:
        return {}

    async def _chosen(ctx: ToolContext, want: str) -> tuple[str | None, str | None]:
        """`(serial, refusal)`. A refusal is a sentence for the owner, not an error:
        it names the radio they have to plug back in, or the double-dedication they
        have to undo, and the tool returns it instead of taking a different radio."""
        if pick_radio is None:
            return None, None
        choice = await pick_radio(ctx.session, want)
        return choice.serial, refusal(choice)

    async def _call(path: str, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            resp = await client.post(path, json=params)
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text[:300]}
        return resp.status_code, body

    async def _get(path: str) -> tuple[int, dict[str, Any]]:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            resp = await client.get(path)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.status_code, body

    async def sdr_listen(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        try:
            mhz = float(arguments.get("frequency_mhz") or 0)
        except (TypeError, ValueError):
            return "That frequency isn't a number — give it in MHz, like 99.3."
        refusal = out_of_range(mhz)
        if refusal:
            return refusal

        mode = str(arguments.get("mode") or _default_mode(mhz)).lower()
        if mode not in MODES:
            return f"I don't know the mode {mode!r} — try one of {', '.join(MODES)}."

        serial, refused = await _chosen(ctx, GENERAL)
        if refused is not None:
            return refused
        status, body = await _call(
            "/listen/start",
            {
                "frequency_hz": int(round(mhz * 1_000_000)),
                "mode": mode,
                "gain": None,
                "serial": serial,
            },
        )
        if status == 409:
            # PASS THE SIDECAR'S REASON THROUGH. It names the radio and the job
            # holding it, and the jobs need opposite advice — "release it to listen"
            # against "release it to log". A hardcoded "already listening" here was
            # worse than generic: while the radio was logging APRS it was simply false,
            # and it overwrote the only answer that told the owner which switch to
            # throw (docs/plans/APRS_CONTROL_PLAN.md P0).
            held = str(body.get("detail") or "the radio is already in use")
            return (
                f"{held[:1].upper()}{held[1:]}. The owner can release it from the "
                "radio icon in the composer, then ask again."
            )
        if status != 200:
            return f"The radio didn't start: {body.get('detail', 'unknown error')}"

        # The owner now has the tuner in their composer; say where it is rather than
        # narrating settings they can see on it.
        return (
            f"Listening on {mhz:g} MHz ({mode.upper()}). The radio icon is in the "
            "composer — tap it to tune, hear it, or release the radio."
        )

    async def sdr_aprs_logging(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        enabled = arguments.get("enabled")
        if not isinstance(enabled, bool):
            return "Say whether to turn APRS logging on or off (enabled: true or false)."

        status, health = await _get("/healthz")
        if status != 200:
            return "The radio isn't reachable, so I can't change APRS logging."
        # An OLDER sidecar ignores an unknown `purpose` and returns 200 with a plain
        # LISTENING session, so "turn logging on" would appear to succeed while logging
        # nothing. Ask what it understands rather than trusting a 200.
        if PURPOSE_APRS not in (health.get("purposes") or []):
            return (
                "This box's radio service is too old to log APRS — it needs the update "
                "that adds packet decoding. Nothing was changed."
            )
        # `sessions`, not `listening`: with a radio each for APRS and the tuner,
        # `listening` is the tuner's — so this reported "APRS logging is already off"
        # while it was running, and turning it "on" started a second one.
        session = session_for(health, PURPOSE_APRS)
        already = bool(session)

        if not enabled:
            if not already:
                return "APRS logging is already off."
            # Stop THIS session by id. Without that, "turn logging off" would release a
            # listening session the owner had started — which on a one-dongle box is the
            # only session there is, and on a two-dongle box is the one the owner can
            # see in the tuner sheet.
            status, body = await _call("/listen/stop", {"session_id": session.get("session_id")})
            if status != 200:
                return f"Couldn't stop APRS logging: {body.get('detail', 'unknown error')}"
            if not body.get("stopped"):
                # 200 with `stopped: false` means the id no longer matched — the session
                # changed between the health read and the stop. Saying "off" here is how
                # a 09:00 "turn logging off" leaves the tuner held for the rest of the
                # day: nothing was stopped and the owner was told otherwise.
                return (
                    "The radio changed while I was stopping it, so APRS logging may "
                    "still be on. Ask me again and I'll re-check."
                )
            return "APRS logging is off. The radio is free."

        if already:
            # Idempotent: a re-run of "turn it on" is a no-op that SUCCEEDS, or a
            # scheduled retry becomes a failure.
            mhz = float(session.get("frequency_hz") or 0) / 1_000_000
            return f"APRS logging is already on, on {mhz:g} MHz."

        try:
            mhz = float(arguments.get("frequency_mhz") or APRS_DEFAULT_MHZ)
        except (TypeError, ValueError):
            return "That frequency isn't a number — give it in MHz, like 144.39."
        refusal = out_of_range(mhz)
        if refusal:
            return refusal

        # Which radio, before taking one. This is the conversational path to logging,
        # so without it "turn APRS on" opens whichever radio enumerated first while the
        # PWA switch beside it refuses — the same act, two answers.
        serial, refused = await _chosen(ctx, PURPOSE_APRS)
        if refused is not None:
            return refused
        status, body = await _call(
            "/listen/start",
            {
                "frequency_hz": int(round(mhz * 1_000_000)),
                "mode": "fm",
                "gain": None,
                "purpose": PURPOSE_APRS,
                "serial": serial,
            },
        )
        if status == 409:
            held = str(body.get("detail") or "the radio is already in use")
            return (
                f"{held[:1].upper()}{held[1:]}. APRS logging needs the radio to itself, "
                "so the owner has to release it first."
            )
        if status != 200:
            return f"APRS logging didn't start: {body.get('detail', 'unknown error')}"
        # The RESULTING state, never "ok": a caller must not be able to report a
        # success it did not achieve.
        return (
            f"APRS logging is on, on {mhz:g} MHz. That radio is held for packets, so it "
            "can't be listened to until logging is turned off — but another dongle, if "
            "this box has one, is still free."
        )

    async def sdr_stop(_arguments: dict, _ctx: ToolContext) -> str | ToolOutput:
        status, body = await _call("/listen/stop", {"session_id": None})
        if status != 200:
            return f"Couldn't release the radio: {body.get('detail', 'unknown error')}"
        if body.get("stopped"):
            return "Radio released."
        # Naming no session means the LISTENING one, and a service is never released
        # this way: "release the radio" must not stop a log the owner armed on a
        # schedule. So say what IS holding one, or the answer is a dead end.
        holding = body.get("holding") or []
        jobs = sorted({str(h.get("purpose")) for h in holding if isinstance(h, dict)})
        if not jobs:
            return "Nothing was listening — the radio is already free."
        return (
            f"Nothing was listening. {' and '.join(jobs)} is holding a radio; that has "
            "its own switch, so tell me which you want turned off."
        )

    async def sdr_read(arguments: dict, _ctx: ToolContext) -> str | ToolOutput:
        """The band table and the radio roster. Takes no radio, so it never refuses.

        One tool for two readings rather than two tools, which is `TOOL_CATALOG_PLAN`
        W1's shape: the catalog is already back at its pre-W1 size, and two more names
        for two lookups is how it got there."""
        what = str(arguments.get("what") or "bands").lower()
        if what == "radios":
            return _radio_roster(await _get("/listen/current"))
        if what != "bands":
            return f"I can read `bands` or `radios`, not {what!r}."
        wanted = arguments.get("section")
        if wanted:
            found = bands.by_id(str(wanted))
            if found is None:
                return f"No section called {wanted!r}. Ask with no section to see the whole table."
            return _section_detail(found)
        return _band_table()

    async def sdr_signal(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        """Power in dBFS, which is a number this system could not produce until F6.

        The only level available before was the loudness of demodulated audio off the
        discriminator — after AGC, squelch and de-emphasis, so not a signal level at
        all and not comparable between the radio's two signal paths (F9)."""
        span = _signal_span(arguments)
        if isinstance(span, str):
            return span
        start_hz, stop_hz = span
        seconds = _bounded_seconds(arguments.get("seconds"))
        serial, refused = await _chosen(ctx, GENERAL)
        if refused is not None:
            return refused
        status, body = await _call(
            "/spectrum/probe",
            {
                "start_hz": start_hz,
                "stop_hz": stop_hz,
                "bin_hz": _signal_bin_hz(start_hz, stop_hz),
                "seconds": seconds,
                "serial": serial,
                **_signal_capture(start_hz, stop_hz),
            },
        )
        if status == 409:
            held = str(body.get("detail") or "the radio is already in use")
            return f"{held[:1].upper()}{held[1:]}. Another radio may be free."
        if status != 200:
            return f"Couldn't measure it: {body.get('detail', 'unknown error')}"
        return _signal_reading(body, start_hz, stop_hz)

    return {
        "sdr_listen": sdr_listen,
        "sdr_stop": sdr_stop,
        "sdr_aprs_logging": sdr_aprs_logging,
        "sdr_read": sdr_read,
        "sdr_signal": sdr_signal,
    }
