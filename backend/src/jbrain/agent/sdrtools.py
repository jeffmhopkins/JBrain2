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

from typing import Any

import httpx

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput

# The R820T2's real range. HF below 24 MHz needs direct sampling and is out of scope.
MIN_MHZ = 0.024
MAX_MHZ = 1766.0
MODES = ("fm", "nfm", "wbfm", "am", "usb", "lsb")

# Broadcast FM is the common case a plain frequency implies, and getting it wrong is
# audible: narrowband on a broadcast station is mush. Anything at or above 88 and
# below 108 defaults to wide FM unless the caller says otherwise.
_BROADCAST_FM = (88.0, 108.0)


def _default_mode(mhz: float) -> str:
    return "wbfm" if _BROADCAST_FM[0] <= mhz < _BROADCAST_FM[1] else "fm"


def build_sdr_handlers(base_url: str) -> dict[str, ToolHandler]:
    """Bind the radio tools to the sidecar. Empty base_url => no radio => no tools,
    the same graceful degrade the image and transcription tools use."""
    if not base_url:
        return {}

    async def _call(path: str, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            resp = await client.post(path, json=params)
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text[:300]}
        return resp.status_code, body

    async def sdr_listen(arguments: dict, _ctx: ToolContext) -> str | ToolOutput:
        try:
            mhz = float(arguments.get("frequency_mhz") or 0)
        except (TypeError, ValueError):
            return "That frequency isn't a number — give it in MHz, like 99.3."
        if not MIN_MHZ < mhz < MAX_MHZ:
            return f"{mhz} MHz is outside what this radio can tune ({MIN_MHZ}–{MAX_MHZ} MHz)."

        mode = str(arguments.get("mode") or _default_mode(mhz)).lower()
        if mode not in MODES:
            return f"I don't know the mode {mode!r} — try one of {', '.join(MODES)}."

        status, body = await _call(
            "/listen/start",
            {"frequency_hz": int(round(mhz * 1_000_000)), "mode": mode, "gain": None},
        )
        if status == 409:
            return (
                "The radio is already listening to something else. The owner can "
                "release it from the tuner in the composer, then ask again."
            )
        if status != 200:
            return f"The radio didn't start: {body.get('detail', 'unknown error')}"

        # The owner now has the tuner in their composer; say where it is rather than
        # narrating settings they can see on it.
        return (
            f"Listening on {mhz:g} MHz ({mode.upper()}). The radio icon is in the "
            "composer — tap it to tune, hear it, or release the radio."
        )

    async def sdr_stop(_arguments: dict, _ctx: ToolContext) -> str | ToolOutput:
        status, body = await _call("/listen/stop", {"session_id": None})
        if status != 200:
            return f"Couldn't release the radio: {body.get('detail', 'unknown error')}"
        return "Radio released." if body.get("stopped") else "The radio wasn't listening."

    return {"sdr_listen": sdr_listen, "sdr_stop": sdr_stop}
