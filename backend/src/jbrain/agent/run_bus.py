"""Announces agent turns as they start and end, for a PWA that did not start them.

A chat the PWA started streams into it over that POST's own SSE. A turn started anywhere else
— the desk panel's spoken turn, a plan continuation — ran invisibly in an open chat until it
was reopened (owner, 2026-10-12: "my panel sending it a message didn't cause an update on the
PWA conversation while I had it up"). Every turn that registers in `app.state.live_turns`
publishes here, and `GET /api/chat/events` streams it to the open PWA, which attaches to the
run's live stream (`/chat/runs/{id}/stream`) exactly as a reload reattach does — and refreshes
its session list, so a new "Desk panel" chat appears without a pull.

Each event names its principal and a subscriber sees only its own: a session id is the owner's
metadata, and the debug console's owner token is a different principal from the PWA login.
Ephemeral and in-process by design: a dropped stream loses nothing the transcript does not hold,
and the client's existing poll and visibility refresh cover the gap.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Literal

RunPhase = Literal["started", "ended"]


@dataclass(frozen=True)
class RunEvent:
    principal_id: str
    session_id: str
    run_id: str
    phase: RunPhase

    def wire(self) -> dict[str, str]:
        """What a subscriber is sent: never the principal, which it already is."""
        return {"type": f"run_{self.phase}", "session_id": self.session_id, "run_id": self.run_id}


class RunBus:
    """Fan-out of RunEvents to each subscriber's bounded queue. A slow subscriber loses its
    oldest events rather than holding up a turn, and publishing never raises."""

    def __init__(self, maxsize: int = 64):
        self._subscribers: dict[asyncio.Queue[RunEvent], str] = {}
        self._maxsize = maxsize

    def subscribe(self, principal_id: str) -> asyncio.Queue[RunEvent]:
        q: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers[q] = principal_id
        return q

    def unsubscribe(self, q: asyncio.Queue[RunEvent]) -> None:
        self._subscribers.pop(q, None)

    def publish(self, event: RunEvent) -> None:
        for q, principal_id in list(self._subscribers.items()):
            if principal_id != event.principal_id:
                continue
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


def announce(
    bus: RunBus | None, principal_id: str | None, session_id: str, run_id: str, phase: RunPhase
) -> None:
    """Publish one turn's start or end. A missing bus or principal is a no-op, and nothing here
    can break the turn that announced itself."""
    if bus is None or not principal_id:
        return
    with contextlib.suppress(Exception):
        bus.publish(RunEvent(str(principal_id), str(session_id), str(run_id), phase))
