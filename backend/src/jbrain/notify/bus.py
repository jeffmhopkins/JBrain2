"""In-process owner-notification fan-out for the self-hosted push feed.

The native owner app holds one SSE connection per device to the notifications stream and
posts a local Android notification per event; server-side subsystems publish here (the
`notify_owner` helper). Unlike the FCM poke path — which is subject-scoped and PII-free
by construction because it routes through Google — this is the OWNER's own device talking
to the OWNER's own server, so events carry their title/body directly with no fetch
round-trip.

Mirrors `locations.live.LiveBroadcaster`: each subscriber gets a bounded queue, and on
overflow the oldest event is dropped, so one slow or stuck connection can neither stall
the others nor grow memory without bound.
"""

import asyncio
import contextlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Notification:
    """One owner notification. `kind` routes the app's handling (and the tap target);
    `ref` is an opaque id the app deep-links to (e.g. a session id); `title`/`body` are
    shown as-is on the device."""

    kind: str
    title: str
    body: str
    ref: str = ""


class NotifyBus:
    def __init__(self, maxsize: int = 128):
        self._subscribers: set[asyncio.Queue[Notification]] = set()
        self._maxsize = maxsize

    def subscribe(self) -> "asyncio.Queue[Notification]":
        q: asyncio.Queue[Notification] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[Notification]") -> None:
        self._subscribers.discard(q)

    def publish(self, note: Notification) -> None:
        for q in self._subscribers:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # drop the oldest to make room
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(note)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


def notify_owner(bus: NotifyBus | None, note: Notification) -> None:
    """Publish `note` to the owner's connected devices — the reusable entry point any
    subsystem calls to raise a notification. A None bus (unconfigured) is a no-op, and a
    publish never raises, so a notification can never break the flow that raised it."""
    if bus is None:
        return
    with contextlib.suppress(Exception):
        bus.publish(note)
