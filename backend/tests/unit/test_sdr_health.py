"""Which session is which, once the box has more than one radio.

MEASURED 2026-09-04: with APRS logging on the long wire and the tuner on the desk whip,
the sidecar holds two sessions. `listening` is now the ONE the omnibox should draw — the
tuner, by preference — so every caller that asked it "is APRS logging?" started answering
no while it was running. Three callers asked exactly that: the PWA's APRS routes, jerv's
`sdr_aprs_logging`, and the packet drain, which would DETACH and drop frames the radio
was still decoding.
"""

from __future__ import annotations

from typing import Any

from jbrain.sdr.health import session_for


def _two_radios() -> dict[str, Any]:
    """What a two-dongle box reports: the tuner is `listening`, APRS is not."""
    return {
        "purposes": ["listen", "aprs", "survey"],
        "listening": {"purpose": "listen", "session_id": "s-tuner", "serial": "09022796"},
        "sessions": [
            {"purpose": "listen", "session_id": "s-tuner", "serial": "09022796"},
            {"purpose": "aprs", "session_id": "s-aprs", "serial": "77192819"},
        ],
    }


def test_it_finds_the_session_listening_did_not_name() -> None:
    assert session_for(_two_radios(), "aprs")["session_id"] == "s-aprs"


def test_it_still_finds_the_one_listening_did_name() -> None:
    assert session_for(_two_radios(), "listen")["session_id"] == "s-tuner"


def test_a_job_nothing_is_doing_is_falsy_rather_than_None() -> None:
    """So a caller asks `if session:` rather than comparing a purpose a second time —
    the second comparison is where the old bug lived."""
    assert session_for(_two_radios(), "survey") == {}


def test_an_older_sidecar_is_read_through_listening() -> None:
    """The api and the sidecar are separate containers and an update restarts them one
    at a time, so for a few seconds one of them is the previous build. That build has at
    most one session, so `listening` is exactly right there."""
    old = {"purposes": ["listen", "aprs"], "listening": {"purpose": "aprs", "session_id": "s1"}}

    assert session_for(old, "aprs")["session_id"] == "s1"
    assert session_for(old, "listen") == {}


def test_an_older_sidecar_holding_nothing_reads_as_nothing() -> None:
    assert session_for({"purposes": ["listen"], "listening": None}, "aprs") == {}


def test_an_unreachable_sidecar_is_not_a_crash() -> None:
    """`_health` returns None on any transport error, and every caller passes that
    straight in."""
    assert session_for(None, "aprs") == {}


def test_a_malformed_payload_degrades_rather_than_raising() -> None:
    """This runs on the drain's poll loop and on a route the PWA polls. A sidecar
    answering something unexpected must cost an answer, not the loop."""
    for junk in ({"sessions": "nope"}, {"sessions": ["a", 5, None]}, {}, {"sessions": []}):
        assert session_for(junk, "aprs") == {}
