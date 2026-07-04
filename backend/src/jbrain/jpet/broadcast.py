"""In-process fan-out of pet state to connected surfaces (docs/plans/JPET_PLAN.md W1).

The pet is server-authoritative: the drives tick and `/pet/command` mutate the one
`pet_state` row, and every change is published here so the Wall and the phone Control
screen — any number of `GET /pet/stream` subscribers — re-render in sync. Mirrors the
locations `LiveBroadcaster`: each subscriber gets a bounded queue and a slow one drops
its oldest state (a superseded snapshot is fine — the next one is the truth)."""

import asyncio
import contextlib

from jbrain.jpet.service import PetStateInfo


class PetBroadcaster:
    """Fan-out of `PetStateInfo` snapshots to subscribed streams."""

    def __init__(self, maxsize: int = 64):
        self._subscribers: set[asyncio.Queue[PetStateInfo]] = set()
        self._maxsize = maxsize

    def subscribe(self) -> "asyncio.Queue[PetStateInfo]":
        q: asyncio.Queue[PetStateInfo] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[PetStateInfo]") -> None:
        self._subscribers.discard(q)

    def publish(self, state: PetStateInfo) -> None:
        for q in self._subscribers:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # drop the oldest to make room
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(state)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
