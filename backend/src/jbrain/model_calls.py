"""Every model call the box makes, open while it runs, so the vitals page can name what is busy.

The vitals surface used to explain a pinned GPU from two lists: the agent-turn roster and the
box's own events (loads, renders, prefills). Neither sees most of what actually runs. A
GrowBot creature thinks through its own OpenAI proxy (`api.growbot_llm`), code mode and an
external coder through theirs, and the worker's ingest, vision and wiki jobs call the router
with no agent turn around them. So the page read "No agent turns" under a trace at 98% while
two Daves were thinking, and guessed "an image, or loading a model" (owner, 2026-11-18).

This is the layer every one of those passes through: the router's three entry points and the
three proxies that forward straight to the gateway. Each call opens a row when it starts and
settles it when it ends, tagged with WHO asked (the caller and its label, a persona for a
creature) and which model ran it.

Its own table rather than `app.box_events`: that one is sized for a handful of rows an hour
and read whole, and a thinking creature alone makes thousands of these. The read here groups
in SQL and returns only the newest rows individually, so the page's 1 Hz poll stays small
however busy the box is.

Narration, never part of the work: the writes run on a background task the call never awaits,
and every failure is swallowed. A database hiccup can cost a row, never a reply.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy import text

from jbrain.db.session import SessionContext, scoped_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

# Callers. Free text in the column; named here so the writers and the page agree.
ASSISTANT = "assistant"
CREATURE = "creature"
CODE = "code"
EXTERNAL = "external"
PET = "pet"
BACKGROUND = "background"

# How long rows are kept. The page's widest window is fifteen minutes; a day leaves the record
# readable after the fact.
RETENTION = timedelta(days=1)

# A row still open after this belongs to a process that died under it, and is no longer listed.
# Far past any real call (a long generation runs minutes), so a call that is still running is
# never dropped while it pins the GPU. A process that
# restarts settles its own leftovers at once (`settle_orphans`), so this only matters for one
# that died and never came back.
STALE_AFTER = timedelta(hours=2)

# The newest rows the page gets one by one. Everything else in the window is in the groups.
RECENT_LIMIT = 60

_WRITE_TIMEOUT_SECONDS = 5.0
_TEXT_MAX = 200

_maker: async_sessionmaker[AsyncSession] | None = None
_source = ""
_CTX = SessionContext(principal_id="box", principal_kind="owner")
# The narration tasks in flight, held so the loop does not collect one before it lands.
_TASKS: set[asyncio.Task[None]] = set()

# What a router task is, in the owner's words. A task missing here still lists, by its name.
_TASK_LABELS: dict[str, str] = {
    "agent.turn": "assistant turn",
    "agent.vision": "looking at an image",
    "browse.step": "browsing a page",
    "research.title": "titling a report",
    "pet.turn": "pet answering",
    "pet.thought": "pet daydreaming",
    "pet.statue": "pet building a statue",
    "vision.ocr": "reading text in an image",
    "vision.caption": "captioning an image",
    "video.summarize": "summarizing a video",
    "wiki.rewrite": "writing a wiki article",
    "wiki.ground": "checking a wiki article",
    "wiki.lint.contradiction": "wiki health check",
    "wiki.lint.stale": "wiki health check",
    "triage.classify": "sorting the inbox",
    "fact.adjudicate": "weighing a fact",
    "entity.disambiguate": "matching an entity",
    "correction_note.extract": "reading a correction",
    "intake.materialize": "reading an intake",
}
_ASSISTANT_TASKS = frozenset({"agent.turn", "agent.vision", "browse.step", "research.title"})


def configure(maker: async_sessionmaker[AsyncSession], *, source: str) -> None:
    """Wire the writer for this process; `source` names it ("api" / "worker")."""
    global _maker, _source
    _maker = maker
    _source = source


def reset() -> None:
    """Unwire the writer (tests, shutdown): every call goes back to recording nothing."""
    global _maker, _source
    _maker = None
    _source = ""


def router_caller(task: str) -> tuple[str, str]:
    """The (caller, label) a router task is listed under."""
    label = _TASK_LABELS.get(task, task.replace(".", " ").replace("_", " "))
    if task in _ASSISTANT_TASKS:
        return ASSISTANT, label
    if task.startswith("pet."):
        return PET, label
    return BACKGROUND, label


def _clip(value: str) -> str:
    return value if len(value) <= _TEXT_MAX else value[: _TEXT_MAX - 1] + "…"


# Both timestamps are taken in Python when the call starts and ends, never `now()` at write
# time: the writes run on a background task that may wait for a pooled connection, and a row
# stamped by its write would start late and end with a near-zero duration.
_INSERT = """
    INSERT INTO app.model_calls
        (id, started_at, caller, label, task, model, local, persona_id, robot, source)
    VALUES (:id, :started_at, :caller, :label, :task, :model, :local, :persona_id, :robot,
            :source)
"""

_SETTLE = """
    UPDATE app.model_calls SET ended_at = :ended_at, status = :status, error = :error
    WHERE id = :id
"""

_ORPHANS = """
    UPDATE app.model_calls SET ended_at = started_at, status = 'stopped',
        error = 'the process restarted during the call'
    WHERE source = :source AND ended_at IS NULL
"""


async def _write(sql: str, params: dict[str, Any]) -> None:
    if _maker is None:
        return
    try:
        async with asyncio.timeout(_WRITE_TIMEOUT_SECONDS):
            async with scoped_session(_maker, _CTX) as session:
                await session.execute(text(sql), params)
    except Exception:  # noqa: BLE001 - narration must never break the call it narrates
        log.warning("model_calls.write_failed", exc_info=True)


async def _narrate(row: dict[str, Any], done: asyncio.Event, outcome: dict[str, Any]) -> None:
    # One task per call, so the settle can never land before the insert it updates.
    await _write(_INSERT, row)
    await done.wait()
    await _write(_SETTLE, {"id": row["id"], **outcome})


async def settle_orphans() -> None:
    """Settle the rows this process's previous life left open, as `stopped`.

    Called once at startup, after `configure`. A call cut off by a restart (Ops → Update)
    never settles, and without this it would read as running until it aged out. Only one
    process carries each source name, so every open row under it is from a life that ended.
    Ended at its own start, not now: when the process died is unknown, and the restart time
    would credit the call with GPU time the box spent down."""
    await _write(_ORPHANS, {"source": _source})


class Call:
    """The open call, for a caller whose failure arrives in-band (a proxy writes the gateway's
    refusal into a 200 body) rather than as an exception through the block."""

    def __init__(self, outcome: dict[str, Any]) -> None:
        self._outcome = outcome

    def failed(self, reason: str) -> None:
        self._outcome["status"] = "failed"
        self._outcome["error"] = _clip(reason)


@asynccontextmanager
async def track(
    caller: str,
    label: str,
    *,
    model: str,
    task: str = "",
    local: bool = True,
    persona_id: str | None = None,
    robot: str = "",
) -> AsyncIterator[Call]:
    """Record one model call for the duration of the block.

    Never awaits a write: the row is opened and settled by a background task, so the call
    pays for creating a task and nothing else. A call the caller abandons (an owner's Stop,
    a phone hanging up, a caller's timeout) settles as `stopped` rather than being left open
    to age out, because setting the event below is synchronous and still works inside a
    cancelled scope."""
    outcome: dict[str, Any] = {"status": "ok", "error": "", "ended_at": None}
    if _maker is None:
        yield Call(outcome)
        return
    done = asyncio.Event()
    row = {
        "id": uuid.uuid4(),
        "started_at": datetime.now(tz=UTC),
        "caller": caller,
        "label": _clip(label),
        "task": task,
        "model": _clip(model),
        "local": local,
        "persona_id": persona_id,
        "robot": robot,
        "source": _source,
    }
    narration = asyncio.get_running_loop().create_task(_narrate(row, done, outcome))
    _TASKS.add(narration)
    narration.add_done_callback(_TASKS.discard)
    try:
        yield Call(outcome)
    except (asyncio.CancelledError, GeneratorExit):
        outcome["status"] = "stopped"
        raise
    except BaseException as exc:
        outcome["status"] = "failed"
        outcome["error"] = _clip(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        outcome["ended_at"] = datetime.now(tz=UTC)
        done.set()


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
            text("DELETE FROM app.model_calls WHERE started_at < :cut"),
            {"cut": moment - RETENTION},
        )
    return cast("Any", result).rowcount or 0


# Rows overlapping the window: settled inside it, or still open and not abandoned. Each arm
# has its own index (migration 0275), so a poll reads the window's rows and the open ones,
# not every row since the oldest call that could still be live.
_WINDOW = """
    FROM app.model_calls m
    LEFT JOIN app.growbot_personas p ON p.id = m.persona_id
    WHERE m.ended_at >= :cut OR (m.ended_at IS NULL AND m.started_at >= :stale)
"""

# Who the row is for: a creature by its persona's name, else its robot, else the label alone.
_WHO = "COALESCE(p.name, NULLIF(m.robot, ''), '')"

_ROW = f"""
    SELECT m.id, m.started_at, m.ended_at, m.caller, {_WHO} AS who, m.label, m.model, m.local,
           m.status, m.error, m.source
"""


def _ms(at: datetime | None) -> int | None:
    return int(at.timestamp() * 1000) if at is not None else None


def _call(r: Any) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "started_ms": _ms(r.started_at),
        "ended_ms": _ms(r.ended_at),
        "caller": r.caller,
        "who": r.who,
        "label": r.label,
        "model": r.model,
        "local": bool(r.local),
        "status": r.status,
        "error": r.error,
        "source": r.source,
    }


async def window(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    seconds: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The model calls overlapping the trailing window: every one still running (on its own,
    so a long call is never pushed out by a busy creature's newer ones), a group per (caller,
    who, label, model) with how many ran and how long they held a model inside the window, and
    the newest RECENT_LIMIT rows one by one. Times are epoch milliseconds, like the vitals
    history the page plots them against."""
    moment = now or datetime.now(tz=UTC)
    cut = moment - timedelta(seconds=seconds)
    stale = moment - STALE_AFTER
    params = {"cut": cut, "stale": stale, "now": moment}
    async with scoped_session(maker, ctx) as session:
        groups = (
            await session.execute(
                text(
                    f"""
                    SELECT m.caller, {_WHO} AS who, m.label, m.model, bool_or(m.local) AS local,
                           count(*) AS calls,
                           count(*) FILTER (WHERE m.ended_at IS NULL) AS running,
                           count(*) FILTER (WHERE m.status = 'failed') AS failed,
                           sum(extract(epoch FROM
                               LEAST(COALESCE(m.ended_at, :now), :now)
                               - GREATEST(m.started_at, :cut))) AS busy_s,
                           max(m.started_at) AS last_at
                    {_WINDOW}
                    GROUP BY m.caller, who, m.label, m.model
                    ORDER BY last_at DESC
                    """  # noqa: S608 - fixed SQL fragments, every value is bound
                ),
                params,
            )
        ).all()
        running = (
            await session.execute(
                text(
                    f"""
                    {_ROW}
                    FROM app.model_calls m
                    LEFT JOIN app.growbot_personas p ON p.id = m.persona_id
                    WHERE m.ended_at IS NULL AND m.started_at >= :stale
                    ORDER BY m.started_at
                    """  # noqa: S608 - fixed SQL fragments, every value is bound
                ),
                {"stale": stale},
            )
        ).all()
        rows = (
            await session.execute(
                text(
                    f"""
                    {_ROW}
                    {_WINDOW}
                    ORDER BY m.started_at DESC
                    LIMIT :limit
                    """  # noqa: S608 - fixed SQL fragments, every value is bound
                ),
                {**params, "limit": RECENT_LIMIT},
            )
        ).all()

    out_groups = [
        {
            "caller": g.caller,
            "who": g.who,
            "label": g.label,
            "model": g.model,
            "local": bool(g.local),
            "calls": int(g.calls),
            "running": int(g.running),
            "failed": int(g.failed),
            "busy_ms": max(0, round(float(g.busy_s or 0) * 1000)),
            "last_ms": _ms(g.last_at),
        }
        for g in groups
    ]
    return {
        "groups": out_groups,
        "running": [_call(r) for r in running],
        "calls": [_call(r) for r in rows],
    }
