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
from jbrain.sdr.roles import Choice, choose
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


async def for_purpose(
    client: Any, token: str, store: Any, ctx: SessionContext, want: str
) -> Choice:
    """Which radio `want` should open, and the sentence explaining it.

    The whole Choice comes back rather than a serial, because `serial is None` covers
    situations needing opposite handling: `unknown` means the scan could not see, so the
    sidecar should proceed exactly as it always has, while `waiting` and `ambiguous`
    mean the OWNER has to plug something in or stop double-dedicating, and must never
    read as "the radio is busy"."""
    attached = await attached_serials(client, token)
    if attached is None:
        return Choice(None, "unknown", "")
    return choose(await store.sdr_radios(ctx), attached, want)


#: Choices the caller cannot fix by retrying: the owner has to act. Every entry point
#: that takes a radio turns these into a refusal naming the radio, rather than starting
#: a session on a different one.
OWNER_MUST_ACT = ("waiting", "ambiguous", "none")


def refusal(choice: Choice) -> str | None:
    """The sentence to refuse with, or None to go ahead.

    One place decides which reasons are refusals, because three call sites deciding it
    separately is how the fourth one forgets."""
    return choice.detail if choice.reason in OWNER_MUST_ACT else None
