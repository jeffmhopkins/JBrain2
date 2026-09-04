"""jerv's radio tools: take the tuner, and hand it back.

`sdr_listen` is what makes the omnibox radio icon appear. That is not incidental —
the icon exists only while a session holds the tuner (the icon IS the lease,
docs/plans/SDR_RADIO_PLAN.md D7), so a tool that takes the lease is the only thing
that can put the control surface in front of the owner. Without it the tuner sheet
is unreachable: nothing else on the box starts a session.

The box has ONE tuner, so `sdr_listen` returns a plain, recoverable "the radio is
busy" rather than waiting. Telling the model the radio is held lets it say so;
queueing it behind an unknown wait would just look like a hang.

Frequency and mode only — never a URL, and bounded here as well as in the api and
the sidecar. The `stream.py` SSRF guard is untouched by this path (§4.4).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.sdr.health import session_for
from jbrain.sdr.resolve import refusal
from jbrain.sdr.roles import GENERAL, Choice
from jbrain.sdr.tuner import MAX_MHZ, MIN_MHZ

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
APRS_DEFAULT_MHZ = 144.39


def _default_mode(mhz: float) -> str:
    return "wbfm" if _BROADCAST_FM[0] <= mhz < _BROADCAST_FM[1] else "fm"


#: How a tool asks which radio to open: `(session_ctx, purpose) -> Choice`. Injected
#: rather than built here because resolving needs the settings store and the supervisor
#: client, and these tools are constructed with neither.
RadioPicker = Callable[[SessionContext, str], Awaitable[Choice]]


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
        if not MIN_MHZ < mhz < MAX_MHZ:
            return f"{mhz} MHz is outside what this radio can tune ({MIN_MHZ}–{MAX_MHZ} MHz)."

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
            # PASS THE SIDECAR'S REASON THROUGH. It names which job holds the tuner,
            # and the two jobs need opposite advice — "release it to listen" against
            # "release it to log". A hardcoded "already listening" here was worse than
            # generic: while the radio was logging APRS it was simply false, and it
            # overwrote the only answer that told the owner which switch to throw
            # (docs/plans/APRS_CONTROL_PLAN.md P0).
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
        if not MIN_MHZ < mhz < MAX_MHZ:
            return f"{mhz} MHz is outside what this radio can tune ({MIN_MHZ}-{MAX_MHZ} MHz)."

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
            f"APRS logging is on, on {mhz:g} MHz. The radio is held for packets, so it "
            "can't be listened to until logging is turned off."
        )

    async def sdr_stop(_arguments: dict, _ctx: ToolContext) -> str | ToolOutput:
        status, body = await _call("/listen/stop", {"session_id": None})
        if status != 200:
            return f"Couldn't release the radio: {body.get('detail', 'unknown error')}"
        return "Radio released." if body.get("stopped") else "The radio wasn't listening."

    return {
        "sdr_listen": sdr_listen,
        "sdr_stop": sdr_stop,
        "sdr_aprs_logging": sdr_aprs_logging,
    }
