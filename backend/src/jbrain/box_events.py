"""The box's own account of what it is doing to the GPU — model loads, evictions, renders.

The vitals detail surface plots GPU busy % beside a roster of agent turns, and the two
routinely disagree: the trace sits pinned at 94% with nothing in the roster, because the
heaviest work this box does is not a turn. Loading gpt-oss-120b reads tens of GB into
unified memory and pins the iGPU for the better part of a minute; the eviction that made
room for it freed whatever was resident; an image render takes the whole pool. The screen
could only say "the GPU is busy with something else" and leave the owner to guess which
something — this is the record that answers it, and, because a row is opened when the work
STARTS, it answers during the spike ("loading oss120b…") rather than only after.

A TABLE (`app.box_events`, migration 0167), not the in-memory ring the GPU samples use
(jbrain.vitals_ring), because this record has to be CROSS-PROCESS. The api and the worker
each build their own gateway and residency coordinator and each load models, so a
process-local ring would show the owner whichever half happened to run in the process
serving the page — and the confusing loads (a deferred transcription, an ingest) are
mostly the worker's. Volume is a handful of rows an hour, pruned at a day.

Best-effort in the strongest sense: this is a NARRATION of work, never part of it. Every
write swallows its own failure, so a database hiccup can cost a model load one round trip
but can never fail one, and nothing here is ever awaited for its result.

The writer is ambient (module-level `configure`, like the prompt capture) because its call
sites are deep inside `jbrain.llm.local_gateway` — an HTTP client that has no session, no
request, and no business acquiring either. The readers take their maker explicitly, the
ordinary way.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy import text

from jbrain.db.session import SessionContext, scoped_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

# Event kinds. Free text in the column (a new kind must never need a migration), named
# here so the writers and the surface agree on the spelling.
MODEL_LOAD = "model_load"
MODEL_UNLOAD = "model_unload"
IMAGE_RENDER = "image_render"
# The gateway config could not be re-stamped, so llama-swap is serving flags that no longer
# match the saved settings. Surfaced because the re-stamp now happens ONCE, immediately before
# a load (llm.local_gateway._config_regen) instead of on every settings PUT: the old code got a
# retry on the operator's next edit, this one does not, and a model that quietly loads with
# stale flags is exactly the kind of thing that reads as "the model is behaving oddly" weeks
# later. Never fails the load — the model still starts, just not at the settings the meter shows.
GATEWAY_CONFIG_STALE = "gateway_config_stale"

# How long rows are kept. The surface's widest window is fifteen minutes; a day gives the
# debug console something to read back after the fact without the table ever mattering.
RETENTION = timedelta(days=1)

# A row still open after this is not running any more — the process that opened it died
# mid-load (or was restarted under it). Reported as `stale`, never as still loading: a
# screen that says "loading oss120b…" forever is a worse lie than admitting we stopped
# watching. Comfortably past the slowest real load (a cold 80B, ~2 minutes).
STALE_AFTER = timedelta(minutes=10)

_DETAIL_MAX = 200

# See `_write`: a narration write may never outlive the work it narrates.
_WRITE_TIMEOUT_SECONDS = 5.0

# The writer's ambient wiring: set once per process at startup, absent everywhere else
# (tests, the CLI, a one-shot script), where every write is a silent no-op.
_maker: async_sessionmaker[AsyncSession] | None = None
_source = ""

# The owner-kind context every write runs under. Host telemetry is owner-only, and both
# writers (the api's request paths and the worker's jobs) are the owner's own machinery.
_CTX = SessionContext(principal_id="box", principal_kind="owner")

# WHY the current work is happening, set by whichever caller knows. The gateway client
# records the load and the unload, but only the caller knows that this unload is "making
# room for gpt-oss-120b" and that load is "restoring what an image render displaced" —
# which is the whole difference between a log and an explanation. A ContextVar rather than
# a parameter so the reason rides through the `LocalGateway` protocol without widening it:
# a load reason threaded as an argument would have to appear on the protocol, and every
# structural test fake would have to grow it (the same argument that keeps `load_progress`
# off that protocol).
_reason: ContextVar[str] = ContextVar("box_event_reason", default="")


def configure(maker: async_sessionmaker[AsyncSession], *, source: str) -> None:
    """Wire the writer for this process. `source` names it ("api" / "worker") so a
    mystery load can be traced to the half of the box that caused it."""
    global _maker, _source
    _maker = maker
    _source = source


def reset() -> None:
    """Unwire the writer (tests, shutdown) — every write goes back to a no-op."""
    global _maker, _source
    _maker = None
    _source = ""


@contextmanager
def because(reason: str) -> Iterator[None]:
    """Attribute the events recorded inside this block. See `_reason`."""
    token = _reason.set(reason)
    try:
        yield
    finally:
        _reason.reset(token)


def _clip(value: str) -> str:
    return value if len(value) <= _DETAIL_MAX else value[: _DETAIL_MAX - 1] + "…"


async def _write(sql: str, params: dict[str, Any]) -> None:
    """One narration write, which is allowed to fail and never allowed to raise.

    Bounded, because the callers are not free to wait. An eviction runs inside the box's
    cross-process advisory lock, so a write that blocked on an exhausted connection pool
    would hold that lock for the pool's own timeout — narration stalling the very load it
    is describing. A few seconds is orders of magnitude past a healthy insert and far short
    of mattering to a load that reads tens of GB."""
    if _maker is None:
        return
    try:
        async with asyncio.timeout(_WRITE_TIMEOUT_SECONDS):
            async with scoped_session(_maker, _CTX) as session:
                await session.execute(text(sql), params)
    except Exception:  # noqa: BLE001 — narration must never break the work it narrates
        log.warning("box_events.write_failed", exc_info=True)


# Two inserts rather than one with a nullable parameter, so every timestamp on a row comes
# from the SAME clock — the database's. An `ended_at` stamped in Python against an `at`
# stamped by Postgres can land a hair BEFORE it, which reads on screen as an event that
# finished before it started.
_INSERT_INSTANT = """
    INSERT INTO app.box_events (id, at, ended_at, kind, subject, detail, status, source)
    VALUES (:id, now(), now(), :kind, :subject, :detail, :status, :source)
"""

_INSERT_OPEN = """
    INSERT INTO app.box_events (id, at, ended_at, kind, subject, detail, status, source)
    VALUES (:id, now(), NULL, :kind, :subject, :detail, 'running', :source)
"""

_SETTLE = """
    UPDATE app.box_events SET ended_at = now(), status = :status, detail = :detail
    WHERE id = :id
"""


async def record(kind: str, subject: str, *, detail: str | None = None, status: str = "ok") -> None:
    """Record something that happened at an instant — an unload, a refusal. `detail`
    defaults to the ambient reason (`because`)."""
    await _write(
        _INSERT_INSTANT,
        {
            "id": uuid.uuid4(),
            "kind": kind,
            "subject": subject,
            "detail": _clip(detail if detail is not None else _reason.get()),
            "status": status,
            "source": _source,
        },
    )


@asynccontextmanager
async def span(kind: str, subject: str, *, detail: str | None = None) -> AsyncIterator[None]:
    """Record something that TAKES TIME, open for the duration.

    The open row is the point: it is what puts "loading oss120b…" on screen while the GPU
    is pinned, instead of a note explaining the spike a minute after it passed. The exit
    settles the row as `ok` or, if the body raised, `failed` with the error on it — and
    then re-raises, because a failed load is the caller's problem, not this module's.

    CancelledError is deliberately not settled — a load killed by a shutdown leaves its row
    open, and the reader ages it out as `stale` (STALE_AFTER). Awaiting a database write
    while the task is being cancelled would be the wrong thing to try, and "we stopped
    watching" is the honest reading anyway."""
    row = uuid.uuid4()
    reason = detail if detail is not None else _reason.get()
    await _write(
        _INSERT_OPEN,
        {"id": row, "kind": kind, "subject": subject, "detail": _clip(reason), "source": _source},
    )
    try:
        yield
    except Exception as exc:
        await _write(
            _SETTLE,
            {"id": row, "status": "failed", "detail": _clip(f"{reason} {exc}".strip())},
        )
        raise
    await _write(_SETTLE, {"id": row, "status": "ok", "detail": _clip(reason)})


def _status(status: str, at: datetime, ended_at: datetime | None, now: datetime) -> str:
    """`stale` for a row left open by a process that died under it — see STALE_AFTER."""
    if ended_at is None and status == "running" and now - at > STALE_AFTER:
        return "stale"
    return status


async def recent(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    seconds: float,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Events OVERLAPPING the trailing window, oldest first — the same ordering as the graph
    they sit under.

    Overlap, not "started inside": a 90-second load is the explanation for the whole last
    minute of trace whether it is still running or settled ten seconds ago. Selecting on
    start alone made a load vanish from the 1m range at the instant it finished — the row
    went from "loading gpt-oss-120b…" to gone rather than to "loaded gpt-oss-120b", while
    the spike it caused was still on screen above it.

    Epoch milliseconds, matching the vitals history, so the surface can place an event
    against the plot without a timezone or precision argument."""
    moment = now or datetime.now(tz=UTC)
    cutoff = moment - timedelta(seconds=seconds)
    async with scoped_session(maker, ctx) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT at, ended_at, kind, subject, detail, status, source
                    FROM app.box_events
                    WHERE at >= :cut OR ended_at >= :cut OR ended_at IS NULL
                    ORDER BY at
                    """
                ),
                {"cut": cutoff},
            )
        ).all()
    out: list[dict[str, object]] = []
    for row in rows:
        status = _status(row.status, row.at, row.ended_at, moment)
        # A stale row is open only because the process that opened it died — it is not
        # overlapping anything, so it earns its place only while its START is still inside
        # the window. Otherwise a dead process's leftovers would sit at the top of the list
        # forever, claiming to be the most recent thing the box did.
        if status == "stale" and row.at < cutoff:
            continue
        out.append(
            {
                "at_ms": int(row.at.timestamp() * 1000),
                "ended_ms": int(row.ended_at.timestamp() * 1000) if row.ended_at else None,
                "kind": row.kind,
                "subject": row.subject,
                "detail": row.detail,
                "status": status,
                "source": row.source,
            }
        )
    return out


async def in_flight(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    kind: str = MODEL_LOAD,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """The `kind` of work happening RIGHT NOW, or None when nothing is — the narrow read
    behind every surface that says "loading gpt-oss-120b…" in the present tense.

    Separate from `recent` because the question is different. `recent` answers "what
    overlapped the window the graph is showing", which is a list and belongs under a plot;
    this answers "is the box loading something this second", which is one row and belongs
    on a status line. Reading it out of `recent` would mean pulling the whole window and
    filtering in the caller, once per frame per surface.

    Rows older than STALE_AFTER are excluded rather than returned as stale: a status line
    has no way to render "we stopped watching", so the honest degradation is to fall
    silent. The vitals list, which CAN say it, still does (see `recent`).

    Newest-first with a limit of one: only one model loads at a time (the evictor makes
    room before the load starts), but a row left open by a process that died under it can
    still sit inside the staleness window, and the newer row is the true one."""
    moment = now or datetime.now(tz=UTC)
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT at, subject, detail, source
                    FROM app.box_events
                    WHERE kind = :kind
                      AND ended_at IS NULL
                      AND status = 'running'
                      AND at >= :cut
                    ORDER BY at DESC
                    LIMIT 1
                    """
                ),
                {"kind": kind, "cut": moment - STALE_AFTER},
            )
        ).first()
    if row is None:
        return None
    return {
        "at_ms": int(row.at.timestamp() * 1000),
        "subject": row.subject,
        "detail": row.detail,
        "source": row.source,
    }


async def prune(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    now: datetime | None = None,
) -> int:
    """Delete rows past RETENTION. Returns the number removed."""
    moment = now or datetime.now(tz=UTC)
    async with scoped_session(maker, ctx) as session:
        result = await session.execute(
            text("DELETE FROM app.box_events WHERE at < :cut"), {"cut": moment - RETENTION}
        )
    return cast("Any", result).rowcount or 0
