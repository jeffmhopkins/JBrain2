"""Reading the sidecar's `/healthz`, once, for everyone who asks it a question.

The sidecar used to hold ONE session, so `listening` was the session and "is APRS
logging" was `listening.purpose == "aprs"`. Three places wrote that line: the PWA's APRS
routes, jerv's `sdr_aprs_logging` tool, and the packet drain that decides whether to
attach.

With a radio each for APRS and the tuner there are two sessions, and `listening` is now
whichever ONE the omnibox should draw — the tuner, by preference, because that is what a
person opening the sheet is asking about. So the old line answers "APRS is not logging"
the moment the owner opens the tuner: the PWA switch flips itself off in front of them,
jerv reports logging is off, and the drain DETACHES from a packet stream that is still
producing frames — packets heard and dropped, with no error anywhere.

One function, because three copies of a rule are three chances for one of them to keep
reading the old field.
"""

from __future__ import annotations

from typing import Any


def session_for(health: dict[str, Any] | None, purpose: str) -> dict[str, Any]:
    """The session holding a radio for this job, or `{}` — falsy, so callers can ask
    `if session:` rather than comparing a purpose a second time.

    Falls back to `listening` when `sessions` is absent, which means an OLDER sidecar:
    the api and the sidecar are separate containers and an update restarts them one at a
    time, so for a few seconds one of them is the previous build. On that build there is
    at most one session anyway, so reading it is exactly right.
    """
    if not health:
        return {}
    sessions = health.get("sessions")
    if not isinstance(sessions, list):
        one = health.get("listening") or {}
        sessions = [one] if one else []
    for session in sessions:
        if isinstance(session, dict) and session.get("purpose") == purpose:
            return session
    return {}
