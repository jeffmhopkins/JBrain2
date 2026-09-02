"""Verifying a command heard over the air.

docs/plans/APRS_CONTROL_PLAN.md P4. A packet carries a command word and a short code:

    GATE 7K2M9

The word says what to do; the code says it was the owner. Everything here is about the
code, and the whole design follows from one fact: **every guess must be transmitted.**

**Why a short code is enough.** The threat model is online guessing over a radio
channel, not offline brute force. An attacker cannot try codes quickly, quietly, or
anonymously — each attempt is a transmission, which is slow, loud, direction-findable,
and rate-limited by a lockout. Five base32 characters is ~25 bits; against a five-attempt
lockout that is overwhelming, and it is short enough to key into a mobile radio's head
by hand. The design must not REQUIRE a phone app to be usable.

**HMAC over a counter, not a pad.** A one-time pad works but has to be carried; an
HMAC-based code (HOTP's shape) is generated on both ends from one shared key. The
counter is monotonic, so a replay is a spent code — which is what makes it safe that
anyone within range hears the command in clear.

**Look-ahead is mandatory, not a nicety.** A transmission that never decodes advances
the SENDER's counter and not the box's. Without a window the two drift apart on the
first missed packet and the system wedges permanently. So the box tries the next `K`
counters and resynchronises to whichever matched.

**Consume is atomic and forward-only.** On a match the counter moves PAST the matched
value, so the same code can never be accepted twice — including by someone who heard it.

Nothing here trusts the callsign. It is plain bytes in a frame and forges trivially; it
narrows noise and is not evidence (the plan's two trust tiers).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Five base32 characters ~= 25 bits. See the module docstring for why that is enough
# against a channel where every guess is a transmission.
CODE_LENGTH = 5
# Base32 without the letters that misread when spoken or hand-copied. The sender and
# the box must agree on this exactly.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# How far ahead of the box's counter a code is still accepted. Generous, because a
# missed packet is normal on radio and a wedged system is worse than a slightly wider
# window: every candidate still requires the key to produce.
LOOKAHEAD = 20
# Failed attempts before the command stops accepting anything until the owner resets it.
MAX_FAILURES = 5


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of one attempt, and everything the owner is told about it."""

    accepted: bool
    reason: str
    # The counter to store when accepted — always PAST the matched value.
    next_counter: int = 0
    # How far ahead of the box the sender was; a persistent drift is worth seeing.
    skipped: int = 0


def code_for(key: bytes, counter: int) -> str:
    """The code a sender with this key produces for this counter.

    Truncation follows HOTP: take four bytes at a dynamic offset, mask the sign bit, and
    render in the alphabet above. The dynamic offset is what stops a fixed slice of the
    digest being the only thing an attacker ever needs to model."""
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha256).digest()
    offset = digest[-1] & 0x0F
    chunk = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    out = []
    for _ in range(CODE_LENGTH):
        out.append(_ALPHABET[chunk % len(_ALPHABET)])
        chunk //= len(_ALPHABET)
    return "".join(reversed(out))


def normalise(code: str) -> str:
    """What the box compares. Case and spacing are the operator's, not the protocol's."""
    return "".join(ch for ch in code.upper() if ch in _ALPHABET)


def verify(key: bytes, counter: int, offered: str, *, failures: int = 0) -> Verdict:
    """Check one offered code against the window starting at `counter`.

    Returns a Verdict rather than raising: a wrong code is an ordinary event on a shared
    channel — somebody else's traffic, a mis-keyed digit, or an attempt — and every one
    of them has to be reportable to the owner rather than exceptional."""
    if failures >= MAX_FAILURES:
        # Locked out. Note this is checked BEFORE any comparison, so a lockout cannot be
        # worn down by continuing to guess.
        return Verdict(False, "locked out after too many failed attempts")
    candidate = normalise(offered)
    if len(candidate) != CODE_LENGTH:
        return Verdict(False, "not a code")
    for ahead in range(LOOKAHEAD + 1):
        expected = code_for(key, counter + ahead)
        # Constant-time: a timing side channel is far-fetched over a radio link, but the
        # comparison costs nothing to do properly and the habit is worth keeping.
        if hmac.compare_digest(expected, candidate):
            # Forward-only: the counter moves PAST the match, so this code — which
            # everyone in range just heard — can never be accepted again.
            return Verdict(True, "verified", next_counter=counter + ahead + 1, skipped=ahead)
    return Verdict(False, "code did not verify")


def parse_command(info: str) -> tuple[str, str] | None:
    """Split a packet's info field into (command, code), or None if it is not one.

    Deliberately strict: exactly two whitespace-separated words. A packet channel is
    full of other people's traffic, and anything looser would try to verify half of it —
    filling the attempt log with noise and burning the lockout on strangers."""
    parts = info.strip().split()
    if len(parts) != 2:
        return None
    word, code = parts
    if not word.isalnum() or len(word) > 16:
        return None
    return word.upper(), code


def new_key() -> bytes:
    """A fresh shared secret. 32 bytes: the code is short, the key never is."""
    import secrets

    return secrets.token_bytes(32)


def key_to_text(key: bytes) -> str:
    """The key as the owner copies it to the sending side, once."""
    return base64.b32encode(key).decode().rstrip("=")


def key_from_text(text: str) -> bytes:
    padded = text.strip().upper()
    padded += "=" * (-len(padded) % 8)
    return base64.b32decode(padded)


# --- when a command is listening at all ---------------------------------------------

_WEEKDAYS_ANY: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Window:
    """When a command is armed. Empty means always, while the task is enabled.

    Same shape as a repeat schedule — days plus a local time range — answering a
    different question: not when the task RUNS but when it is LISTENING. `repeat` here
    is a security control rather than a convenience: outside its hours the command does
    not exist, which shrinks the attack surface to the times it would actually be used.
    """

    days: tuple[int, ...] = _WEEKDAYS_ANY
    start: str | None = None
    end: str | None = None
    timezone: str = "UTC"


def armed_at(window: Window, when: datetime) -> bool:
    """Whether the window admits this instant, in the window's own timezone.

    Evaluated at VERIFY time and nowhere else. This looked like a job for
    `ActionSpec.precondition` — described as the engine seam for "only run when X
    holds" — and that is the wrong seam: a precondition DEFERS, with a retry that can
    wait indefinitely, because it was built for "the local model is not resident yet".
    A command outside its window must be REFUSED. A deferred gate command is a gate
    that opens hours later, for someone who is no longer there, in response to a
    transmission the owner may not have sent.
    """
    if not window.days and not window.start and not window.end:
        return True
    try:
        local = when.astimezone(ZoneInfo(window.timezone))
    except (ZoneInfoNotFoundError, ValueError):
        local = when.astimezone(UTC)
    # Sunday=0..Saturday=6, matching the task editor's chip row.
    if window.days and ((local.weekday() + 1) % 7) not in window.days:
        return False
    if window.start and window.end:
        now_minutes = local.hour * 60 + local.minute
        start, end = _minutes(window.start), _minutes(window.end)
        if start is None or end is None:
            return True  # a malformed window must not silently lock the owner out
        if start <= end:
            return start <= now_minutes <= end
        # A window that wraps midnight (22:00–02:00) is two ranges, not an empty one.
        return now_minutes >= start or now_minutes <= end
    return True


def _minutes(hhmm: str) -> int | None:
    parts = hhmm.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute
