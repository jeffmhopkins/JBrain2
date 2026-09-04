"""Asking which radio, from anywhere that can take one.

`roles.choose` is the rule and is pure. This is the plumbing around it — read what the
supervisor can see, read what the owner described, decide — and it lives in its own
module because the PWA routes, the debug console and jerv's tools ALL start radio
sessions, and a rule enforced at one of three doors is not enforced.

That was not hypothetical: the first cut wired only the PWA routes, and `sdr_aprs_logging`
(the tool the plan designates as the conversational path — "jerv needs to do it when
asked") went straight to the sidecar with no serial. Asking the assistant to turn logging
on would have opened whichever radio librtlsdr enumerated first, which is the exact
failure the feature exists to remove, reachable by talking to it.
"""

from __future__ import annotations

from typing import Any

import httpx

from jbrain.db.session import SessionContext
from jbrain.sdr.roles import Choice, choose, named
from jbrain.sdr.tuner import serials_in


async def attached_serials(client: Any, token: str) -> list[str] | None:
    """What the supervisor's `/sys` scan can see — or None when it could not see.

    The supervisor is the only container that reads `/sys`, so this is a proxy hop.
    None and `[]` are different answers all the way down: `[]` is a scan that worked and
    found nothing, which a dedicated radio should WAIT on; None is a scan that failed,
    the one case where naming no radio is right because that is what a one-dongle box
    always did."""
    try:
        resp = await client.get("/usb", headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return serials_in(resp.json())
    except (httpx.HTTPError, ValueError, AttributeError, KeyError):
        return None


async def busy_serials(sdr_url: str | None) -> list[str]:
    """The radios the sidecar already has a session on.

    Without this, `choose` handed APRS and the tuner the same `generals[0]` and the
    second caller met a 409 naming the radio it had asked for — two dongles attached and
    one of them idle, which is the whole symptom P0b exists to remove. Only ever
    REORDERS general radios (see `roles.choose`), so being wrong costs a preference
    rather than a substitution.

    Empty on any failure, deliberately: an unreachable sidecar means nothing can start
    anyway, and guessing that everything is busy would turn a transport error into
    "no radio available" — a settings problem the owner does not have.

    Reads `sessions`, falling back to `listening` for the seconds during an update when
    the sidecar is the older build; `health.session_for` explains why those differ."""
    if not sdr_url:
        return []
    try:
        async with httpx.AsyncClient(base_url=sdr_url, timeout=5.0) as client:
            resp = await client.get("/healthz")
        health = resp.json()
        sessions = health.get("sessions")
        if not isinstance(sessions, list):
            one = health.get("listening") or {}
            sessions = [one] if one else []
        return sorted(
            {
                s["serial"]
                for s in sessions
                if isinstance(s, dict) and isinstance(s.get("serial"), str) and s["serial"]
            }
        )
    except (httpx.HTTPError, ValueError, AttributeError, KeyError):
        return []


async def for_purpose(
    client: Any,
    token: str,
    store: Any,
    ctx: SessionContext,
    want: str,
    sdr_url: str | None = None,
    serial: str | None = None,
) -> Choice:
    """Which radio `want` should open, and the sentence explaining it.

    The whole Choice comes back rather than a serial, because `serial is None` covers
    situations needing opposite handling: `unknown` means the scan could not see, so the
    sidecar should proceed exactly as it always has, while `waiting` and `ambiguous`
    mean the OWNER has to plug something in or stop double-dedicating, and must never
    read as "the radio is busy".

    `sdr_url` is optional so a caller that has no sidecar to ask still gets a decision:
    without it the answer is the same one a one-dongle box always got.

    `serial` is the owner pointing at a radio, and it switches the question from "which
    one" to "may that one" (`roles.named`). It comes from a screen where the radio is the
    object rather than from the model or a schedule, which is why it is honoured rather
    than treated as a hint: a tap the api quietly overrode would be worse than a
    refusal."""
    attached = await attached_serials(client, token)
    if attached is None:
        # The scan could not see, so whether the named radio is attached is unknowable.
        # Passing it through anyway is strictly better than the historical "whatever
        # librtlsdr enumerates first": the owner named it, and if it is gone the sidecar
        # fails on a device it can prove is missing rather than opening the wrong one.
        return Choice(serial, "named", "") if serial else Choice(None, "unknown", "")
    stored = await store.sdr_radios(ctx)
    if serial:
        return named(stored, attached, want, serial)
    return choose(stored, attached, want, await busy_serials(sdr_url))


#: Choices the caller cannot fix by retrying: the owner has to act. Every entry point
#: that takes a radio turns these into a refusal naming the radio, rather than starting
#: a session on a different one.
OWNER_MUST_ACT = ("waiting", "ambiguous", "none", "reserved")


def refusal(choice: Choice) -> str | None:
    """The sentence to refuse with, or None to go ahead.

    One place decides which reasons are refusals, because three call sites deciding it
    separately is how the fourth one forgets."""
    return choice.detail if choice.reason in OWNER_MUST_ACT else None
