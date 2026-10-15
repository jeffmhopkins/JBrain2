"""Is this TV free to show a notice? The rule, kept apart from the library so it is testable.

The owner, 2026-10-14: *"it would always have to be idle. Or ambient or whatever"*, and a TV
that is off may be turned on. So a TV is `idle` (cast over it), `standby` (wake it, cast),
`busy` (never covered, paused included: paused still means somebody is in the room) or
`unknown`, which is refused like `busy` — a probe that cannot tell must not guess.

Every reading also lands in a per-TV history, because the open question this spike exists to
answer (docs/mocks/jpanel-cast/README.md) is what a TV reports in each real state, above all
whether an app opened with the remote shows up at all. The owner changes the TVs' states at
leisure; the timeline is read back afterwards.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

#: The Cast "Backdrop" (ambient / idle screen) app. Anything else running is an app.
BACKDROP_APP_ID = "E8C28D3C"
IDLE_APP_IDS = frozenset({BACKDROP_APP_ID})
#: Media states that mean somebody is watching; IDLE and UNKNOWN do not.
ACTIVE_PLAYER_STATES = frozenset({"PLAYING", "PAUSED", "BUFFERING"})

HISTORY_LEN = 400


@dataclass(frozen=True)
class Reading:
    """What one TV said, flattened from the receiver and media status messages."""

    connected: bool = False
    app_id: str | None = None
    display_name: str | None = None
    status_text: str | None = None
    is_stand_by: bool | None = None
    is_active_input: bool | None = None
    volume_level: float | None = None
    player_state: str | None = None
    media_title: str | None = None
    has_status: bool = False


def verdict(r: Reading) -> tuple[str, str]:
    """`(state, why)`. `why` is said in the owner's words, for the PWA row."""
    if not r.connected or not r.has_status:
        return "unknown", "not connected" if not r.connected else "no status yet"
    if r.is_stand_by is True:
        return "standby", "off"
    if r.player_state in ACTIVE_PLAYER_STATES:
        what = r.display_name or r.media_title or "something"
        return "busy", f"{what} · {r.player_state.lower()}"
    if r.app_id is not None and r.app_id not in IDLE_APP_IDS:
        return "busy", f"{r.display_name or r.app_id} open"
    # An external input (a console, a cable box) leaves the cast receiver idle; the TV
    # says so only when it reports CEC, and None means it does not.
    if r.is_active_input is False:
        return "busy", "on another input"
    return "idle", r.display_name or "nothing open"


def castable(state: str) -> bool:
    return state in ("idle", "standby")


class Refused(Exception):
    """A cast the gate turned down; `state` and `why` are the verdict that refused it."""

    def __init__(self, state: str, why: str):
        super().__init__(f"{state}: {why}")
        self.state, self.why = state, why


@dataclass
class History:
    """A bounded timeline of distinct readings and events, thread-safe: pychromecast calls
    its listeners on its own socket threads while the HTTP server reads."""

    entries: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last: Reading | None = None

    def record(self, r: Reading, now: float | None = None) -> bool:
        """Append `r` if it differs from the last reading; True when it did."""
        with self._lock:
            if r == self._last:
                return False
            self._last = r
            state, why = verdict(r)
            self.entries.append(
                {"at": now if now is not None else time.time(), "kind": "status",
                 "state": state, "why": why, **asdict(r)}
            )
            return True

    def note(self, event: str, now: float | None = None, **detail: Any) -> None:
        with self._lock:
            self.entries.append(
                {"at": now if now is not None else time.time(), "kind": "event",
                 "event": event, **detail}
            )

    def since(self, at: float = 0.0) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self.entries if e["at"] > at]
