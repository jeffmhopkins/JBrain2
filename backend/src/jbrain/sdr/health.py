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

#: Which session an owner should SEE when a box holds several, best first. Serial only
#: BREAKS A TIE, so the answer is deterministic without being arbitrary.
#:
#: **This lives here, not in the sidecar** (B7). It is presentation policy — it decides
#: one composer icon — and it was being decided in the radio process, three purposes
#: deep, feeding a field the api reshaped anyway. The api already holds every session;
#: deciding here is one policy rather than two that can disagree.
SHOWN_FIRST = ("listen", "aprs", "spectrum")


def shown(sessions: list[Any]) -> dict[str, Any] | None:
    """The one session to put in front of a person, out of every live one."""
    rows = [s for s in sessions if isinstance(s, dict)]
    if not rows:
        return None
    rank = len(SHOWN_FIRST)

    def order(session: dict[str, Any]) -> tuple[int, str]:
        purpose = str(session.get("purpose") or "")
        place = SHOWN_FIRST.index(purpose) if purpose in SHOWN_FIRST else rank
        return place, str(session.get("serial") or "")

    return min(rows, key=order)


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
