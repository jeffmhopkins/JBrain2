"""Enrolling a phone by QR code on the box's own screen (docs/plans/OWNER_TUNNEL_PLAN.md §3.5).

Arming opens a two-minute window and reveals nothing, so it can be pressed from anywhere. The
window's single-use code reaches a phone only as a QR on the box's monitor: the wall fetches
it over the internal network and shows it on a loopback-only port. Standing at the box is the
proof.

The code lives only in this process's memory, never on disk or in the database, so nothing
`/sql` or a backup can read derives it, and a restart simply closes the window. The api runs
one process, so this is the one copy.

Both directions of the exchange are bound to the code with labelled MACs: the phone's over
its certificate request, the box's over the phone's MAC and everything it hands back. The
labels stop either side's MAC being reflected as the other's.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jbrain.owner_tunnel.words import WORDS

WINDOW = timedelta(minutes=2)
#: The QR's format version; the app refuses one it does not know.
FORMAT = 1
#: Words a fingerprint is read out in: 40 bits, plenty against a phone swapped in by someone
#: who also has to win the race for a code that lives two minutes.
FINGERPRINT_WORDS = 5

_APP_TO_BOX = b"jbrain-enroll app->box\0"
_BOX_TO_APP = b"jbrain-enroll box->app\0"
_B64URL = re.compile(r"[A-Za-z0-9_-]*")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    """Strict: ValueError on anything but unpadded or padded URL-safe base64."""
    # urlsafe_b64decode silently drops characters outside the alphabet, and b64decode's
    # altchars still accept + and /, so the alphabet is checked here.
    body = text.rstrip("=")
    if not _B64URL.fullmatch(body) or len(body) % 4 == 1:
        raise ValueError("not URL-safe base64")
    return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))


def fingerprint(key_hash: str) -> tuple[str, ...]:
    """A device key as words, for the owner to compare on the box's screen and the phone."""
    return tuple(WORDS[b] for b in bytes.fromhex(key_hash)[:FINGERPRINT_WORDS])


def app_mac(code: bytes, csr_pem: bytes) -> bytes:
    return hmac.new(code, _APP_TO_BOX + csr_pem, hashlib.sha256).digest()


def box_mac(code: bytes, app_tag: bytes, *issued: bytes) -> bytes:
    """Over the phone's own MAC and every certificate handed back, in order, so the reply
    is bound to that one request and nothing in it can be swapped."""
    payload = _BOX_TO_APP + app_tag
    for part in issued:
        payload += len(part).to_bytes(4, "big") + part
    return hmac.new(code, payload, hashlib.sha256).digest()


def qr_payload(
    code: bytes, *, owner_ca_pin: str, lan_url: str, lan_root_pin: str, public_url: str
) -> str:
    """What the QR holds: the code, where to enroll, and the keys to trust on the way."""
    return json.dumps(
        {
            "v": FORMAT,
            "code": b64url(code),
            "ca": owner_ca_pin,
            "lan": lan_url,
            "lanRoot": lan_root_pin,
            "public": public_url,
        },
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class Enrolled:
    device_id: str
    words: tuple[str, ...]


@dataclass
class _Window:
    code: bytes
    expires_at: datetime
    claimed: bool = False
    enrolled: Enrolled | None = None


@dataclass(frozen=True)
class Snapshot:
    """What the box's screen shows. `code` is set only while the window is open and unused."""

    state: str  # "idle" | "armed" | "enrolled"
    expires_at: datetime | None = None
    code: bytes | None = None
    enrolled: Enrolled | None = None


class EnrollmentWindow:
    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now
        self._window: _Window | None = None

    def _live(self) -> _Window | None:
        if self._window is not None and self._window.expires_at <= self._now():
            self._window = None
        return self._window

    def arm(self) -> datetime:
        """Open a fresh window, replacing any open one and its code."""
        self._window = _Window(secrets.token_bytes(32), self._now() + WINDOW)
        return self._window.expires_at

    def close(self) -> None:
        self._window = None

    def snapshot(self) -> Snapshot:
        window = self._live()
        if window is None:
            return Snapshot("idle")
        if window.enrolled is not None:
            return Snapshot("enrolled", window.expires_at, enrolled=window.enrolled)
        if window.claimed:
            return Snapshot("armed", window.expires_at)
        return Snapshot("armed", window.expires_at, code=window.code)

    def claim(self, matches: Callable[[bytes], bool]) -> "Claim | None":
        """Use the code once: the claim if the window is open, unused and `matches` it.

        No await between the check and the mark, so two requests on the event loop cannot
        both claim it. A failed match leaves the window open: the code is 256 bits, so a
        wrong guess costs the guesser nothing and the owner nothing either."""
        window = self._live()
        if window is None or window.claimed or not matches(window.code):
            return None
        window.claimed = True
        # The phone's insert is in flight; a claim in the window's last moment must not
        # lapse before its words are recorded for the owner to compare.
        window.expires_at = max(window.expires_at, self._now() + WINDOW)
        return Claim(window)

    def in_use(self) -> bool:
        """A phone has spent the code: its words are pending or showing, for the box only
        to clear."""
        window = self._live()
        return window is not None and window.claimed

    def record(self, claim: "Claim", enrolled: Enrolled) -> None:
        """Show the new phone's fingerprint for the rest of the window it was claimed from.
        Never another one: the window may have been re-armed and claimed by a second phone
        while this one's insert was in flight, and the owner must see that phone's words."""
        # Identity: a close or re-arm since the claim drops this record. The claim already
        # extended the window by a full WINDOW, so only an insert slower than that, with the
        # screen purging the lapsed window meanwhile, loses its words; the phone then shows
        # in Settings -> Devices, where it can be revoked.
        if self._window is claim.window:
            claim.window.enrolled = enrolled
            # A phone that enrolled in the window's last seconds still gets a full window
            # for the owner to compare the words and reject a mismatch.
            claim.window.expires_at = max(claim.window.expires_at, self._now() + WINDOW)

    def abandon(self, claim: "Claim") -> None:
        """The claimed enrollment failed: close its window so the screen asks for a new one
        rather than sitting on "armed" with no code."""
        if self._window is claim.window:
            self._window = None


@dataclass(frozen=True)
class Claim:
    window: _Window

    @property
    def code(self) -> bytes:
        return self.window.code
