"""Capture what llama-server's `/slots` says WHILE a turn is stuck in prefill.

The chat status line can say a model is loading and how far in it is, but it cannot yet
say anything about the wait AFTER the load: a long prompt on a big local model spends tens
of seconds in prompt evaluation before the first token, and the screen has nothing to show
for it. The reading exists — llama-server tracks prefill per slot and serves it on `/slots`
— but the FIELD NAMES vary by build, and this box runs a community llama.cpp image pinned
to a digest off master (deploy/Dockerfile.local-llm), so the names cannot be looked up in a
release note. Nothing in this repo has ever read a slot body.

Guessing that schema is precisely how the load percentage got shipped dead three times
(`load_progress`, deleted in fe774e4). So this ships the instrument BEFORE the parser: the
next slow turn on the live box records the real shape into the log, the owner reads it back
through `GET /api/debug/logs/{api,worker}` with no terminal, and the parser gets written
once, against evidence. Same move as `footprint_unparsed` (cabbbec), which settled a
two-guess argument on its first deploy.

This module is SCAFFOLDING and should not outlive the answer: once a capture names the
fields, the parser replaces it and this file goes. Left in place it would be a second reader
of `/slots` competing with the real one — which is the shape of the drift that put a load
percentage on two surfaces that could disagree.

SAMPLED, not snapshotted, because one reading cannot say which number is the progress. Three
samples a couple of seconds apart show which fields ADVANCE while the prompt is being eaten
— and a turn whose first token lands mid-series shows the same slot in the decode state,
which is what distinguishes a prefill counter from a generation counter. Bounded at three
because the shape is the deliverable; a minute-long prefill does not teach more than its
first six seconds.

REDACTED BY CONSTRUCTION. `/slots` carries the in-flight PROMPT, which on this box is the
owner's notes — health, finance, location, the domains Postgres has firewalls for — and
these lines land in a log the debug API serves back. Since the schema is unknown, redaction
cannot be a deny-list of the fields that happen to carry text today: `_shape` keeps every
key and every number and replaces every string with its type and length, so a field named
something nobody predicted still cannot leak its contents. Long lists collapse to a count
for the same reason — a prompt also travels as an array of token ids, which is reversible.
That leaves exactly what a parser needs (which keys exist, which integers move) and nothing
a reader could reconstruct a note from.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

import structlog

log = structlog.get_logger()

# Reads `/slots` for one served model — `LocalGatewayClient.slots`, narrowed to a callable so
# the router does not have to depend on the gateway (or widen `LocalAdmitter`, which every
# structural test fake would then have to grow).
SlotsReader = Callable[[str], Awaitable[list[dict[str, object]]]]

# Seconds to wait before each capture, and so also how many there are. The first entry is what
# counts as prefill worth photographing: a warm turn on this box answers in well under a
# second, so three is past anything the KV cache can serve from a primed prefix and safely
# inside the tens of seconds a cold long prompt actually takes. The rest pace the series that
# shows which fields MOVE — the reason there is a series at all.
_SCHEDULE = (3.0, 2.0, 2.0)

# The read is instrumentation, so it may never become part of the wait it measures. Short and
# absolute: the gateway's own client allows up to 180 s for a diagnostic GET, which is the
# right budget for an operator's deliberate probe and far too long for this one.
_READ_TIMEOUT_S = 5.0

# How many slots one sample reports. `-np` is 1 on this box, so this only bounds a future
# multi-slot config; the count of the rest is logged either way.
_SLOTS_LOGGED = 4

# Beyond this a list is reported as a count. See the module docstring: bounds the line, and
# blocks the token-id array that a prompt travels as.
_LIST_KEEP = 8


def _shape(value: object) -> object:
    """The structure of a slot body with none of its content — see the module docstring."""
    if isinstance(value, dict):
        return {str(k): _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return f"<list:{len(value)}>" if len(value) > _LIST_KEEP else [_shape(v) for v in value]
    if isinstance(value, str):
        return f"<str:{len(value)}>"
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return f"<{type(value).__name__}>"


async def _capture(read: SlotsReader, served_model: str, sample: int) -> None:
    try:
        async with asyncio.timeout(_READ_TIMEOUT_S):
            slots = await read(served_model)
    except Exception:  # noqa: BLE001 — a diagnostic may never fail the turn it is watching
        # Loud, not swallowed: an instrument that reports nothing is indistinguishable from a
        # box that never went slow, which is the failure mode this whole module exists to end.
        log.warning("llm.prefill_slots_failed", model=served_model, sample=sample, exc_info=True)
        return
    log.info(
        "llm.prefill_slots",
        model=served_model,
        sample=sample,
        n_slots=len(slots),
        # Serialized here rather than handed over as a list, so the capture reads identically
        # through both renderers it can meet: stdout's JSON, and the run-log tap, which
        # stringifies any non-primitive value it is given (jbrain.log_capture._json_safe).
        slots=json.dumps([_shape(s) for s in slots[:_SLOTS_LOGGED]]),
    )


async def _sample(
    read: SlotsReader, served_model: str, arrived: asyncio.Event, schedule: Sequence[float]
) -> None:
    for index, delay in enumerate(schedule):
        try:
            await asyncio.wait_for(arrived.wait(), delay)
        except TimeoutError:
            await _capture(read, served_model, index)
        else:
            return  # The turn answered inside the window; nothing here was prefill.


@asynccontextmanager
async def watch(
    read: SlotsReader | None, served_model: str, *, schedule: Sequence[float] | None = None
) -> AsyncIterator[Callable[[], None]]:
    """Photograph `/slots` while `served_model` is slow to say anything.

    Yields the callback that ends the watch — the caller invokes it on the first streamed
    part, and whatever samples have not fired never do. `read=None` (a cloud route, local
    hosting off, a bare test router) starts no task at all, so the probe costs nothing on
    every box that cannot use it. `schedule` overrides `_SCHEDULE` for tests, which cannot
    afford to spend the real three seconds waiting to find out that nothing happened."""
    if read is None:
        yield lambda: None
        return
    arrived = asyncio.Event()
    task = asyncio.create_task(
        _sample(read, served_model, arrived, _SCHEDULE if schedule is None else schedule)
    )
    try:
        yield arrived.set
    finally:
        # Cancelled rather than awaited: this runs on the turn's way out, including when the
        # consumer abandons the stream mid-answer, and a diagnostic must not hold that up.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
