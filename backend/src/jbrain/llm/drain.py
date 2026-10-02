"""Local admission, closed across processes while the on-box engine switches.

The engine switch (docs/plans/FLASH_NEXT_ENGINE_PLAN.md F3a) must not stop an engine under a
call that is still generating, nor let a new call load a model into the engine it is about to
stop. `admission.py` is load arithmetic and the ledger tracks load charges, so neither can say
"nothing new may start". This is that gate: one settings-store row both processes read (the
api and the worker each run their own router and residency coordinator), so a closure written
by the api is honoured by a background job in the worker within `GATE_TTL_S`.

The row carries a wall-clock deadline. A switch that dies mid-flight (an api restart, a crash)
can therefore never leave local inference closed for longer than the deadline, and the api
clears it on boot as well.

What a caller sees while it is closed: the router and residency WAIT up to `ADMISSION_WAIT_S`
for it to reopen, then raise `LocalAdmissionClosedError` — a `ResidencyError`, so the worker
defers the job without burning an attempt and a chat turn gets a sentence, not a stack.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jbrain.llm.residency import ResidencyError

# How long a read of the row is trusted. Short: the drain waits at least this long after
# closing before it trusts that no other process is still admitting on a stale "open".
GATE_TTL_S = 1.0
# How long a local call waits for a closed gate to reopen before refusing. Long enough that a
# call landing in the last seconds of a switch rides through it; short enough that a chat turn
# during a several-minute switch says so instead of hanging.
ADMISSION_WAIT_S = 30.0
POLL_S = 1.0


class LocalAdmissionClosedError(ResidencyError):
    """Local admission is closed (an engine switch is draining or swapping engines)."""


@dataclass(frozen=True)
class Closure:
    reason: str
    # Wall-clock (epoch seconds) after which the closure no longer holds, whatever the row says.
    until: float


def closure_from(value: object, now: float) -> Closure | None:
    """The live closure a stored row describes, or None when admission is open — including for
    a malformed row or one past its deadline, because a settings hiccup must never close local
    inference by accident."""
    if not isinstance(value, dict) or value.get("closed") is not True:
        return None
    until = value.get("until")
    if not isinstance(until, int | float) or until <= now:
        return None
    reason = value.get("reason")
    return Closure(reason=reason if isinstance(reason, str) else "", until=float(until))


def closed_row(reason: str, *, ttl_s: float, now: float) -> dict[str, object]:
    return {"closed": True, "reason": reason, "until": now + ttl_s}


OPEN_ROW: dict[str, object] = {"closed": False}


class AdmissionGate:
    """One per process, shared by its router and residency coordinator."""

    def __init__(
        self,
        load: Callable[[], Awaitable[object]],
        *,
        ttl_s: float = GATE_TTL_S,
        wait_s: float = ADMISSION_WAIT_S,
        poll_s: float = POLL_S,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._load = load
        self._ttl_s = ttl_s
        self._wait_s = wait_s
        self._poll_s = poll_s
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._row: object = None
        self._read_at: float | None = None
        self._refresh: asyncio.Future[None] | None = None
        _LIVE.add(self)

    async def closure(self) -> Closure | None:
        now = self._clock()
        if self._read_at is None or now - self._read_at >= self._ttl_s:
            # One read for a burst of callers: every local call admits through here, and a
            # cold cache would otherwise send each of them to the database at once.
            if self._refresh is None or self._refresh.done():
                self._refresh = asyncio.ensure_future(self._read())
            await asyncio.shield(self._refresh)
            self._read_at = now
        return closure_from(self._row, self._wall())

    async def _read(self) -> None:
        row: object = None
        with contextlib.suppress(Exception):
            row = await self._load()
        self._row = row

    def invalidate(self) -> None:
        # Drop an in-flight read too: it may have started before the write being announced.
        self._read_at = None
        self._refresh = None

    async def wait_open(self) -> bool:
        """False when admission is open now; True once it reopened after a wait. Raises
        `LocalAdmissionClosedError` when it stays closed for `wait_s`."""
        closure = await self.closure()
        if closure is None:
            return False
        deadline = self._clock() + self._wait_s
        while closure is not None:
            if self._clock() >= deadline:
                raise LocalAdmissionClosedError(
                    f"The local engine is switching ({closure.reason}) — local models are "
                    "paused until it finishes; try again in a few minutes."
                )
            await self._sleep(self._poll_s)
            self.invalidate()
            closure = await self.closure()
        return True


_LIVE: weakref.WeakSet[AdmissionGate] = weakref.WeakSet()


def invalidate_cached() -> None:
    """Expire every gate in this process, so a closure written here holds at once."""
    for gate in list(_LIVE):
        gate.invalidate()
