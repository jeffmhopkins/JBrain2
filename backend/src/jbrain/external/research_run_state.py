"""Durable checkpoint for a background deepest-research run (`app.research_run_state`,
migration 0147; docs/plans/DEEPEST_RESEARCH_TOOL_PLAN.md, R5).

The run writes its committed state after each round (`checkpoint`), so a worker/box
restart mid-run rehydrates and CONTINUES from the last committed round (`load`) rather
than re-running the whole thing or losing it. A restarted process claims a run for resume
exactly once (`claim_resume`, the 0138 atomic pattern). All access runs on an RLS-scoped
`external` session — the same corpus domain as the report the run produces — so a scoped
non-owner principal can neither read nor write a run's state (CLAUDE.md rule 3).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

log = structlog.get_logger()

_TERMINAL = ("done", "failed", "cancelled")


def run_state_context(principal_id: str) -> SessionContext:
    """The `external` scope the run-state repo reads/writes under — the same corpus domain
    as `research_reports`, so the run's operational state carries the same firewall (a
    scoped non-owner sees nothing) as the report it produces."""
    return SessionContext(
        principal_id=principal_id,
        principal_kind="owner",
        domain_scopes=("external",),
        owner_scoped=True,
    )


@dataclass(frozen=True)
class RunState:
    """One background run's checkpoint — everything needed to rehydrate and continue."""

    id: str
    run_id: str
    session_id: str | None
    question: str
    status: str
    round: int
    ceiling_tokens: int
    wall_clock_deadline: datetime | None
    spent_tokens: int
    agents_spawned: int
    state: dict[str, Any]
    resumed_at: datetime | None


async def create_run(
    maker: async_sessionmaker,
    ctx: SessionContext,
    *,
    run_id: str,
    session_id: str | None,
    question: str,
    ceiling_tokens: int,
    wall_clock_deadline: datetime | None,
) -> str:
    """Open a run's checkpoint row (`status='running'`, round 0). Returns its id."""
    async with scoped_session(maker, ctx) as session:
        res = await session.execute(
            text(
                "INSERT INTO app.research_run_state"
                " (run_id, session_id, question, ceiling_tokens, wall_clock_deadline)"
                " VALUES (:run_id, CAST(:session_id AS uuid), :question, :ceiling, :deadline)"
                " RETURNING id"
            ),
            {
                "run_id": run_id,
                "session_id": session_id,
                "question": question,
                "ceiling": ceiling_tokens,
                "deadline": wall_clock_deadline,
            },
        )
        return str(res.scalar_one())


async def checkpoint(
    maker: async_sessionmaker,
    ctx: SessionContext,
    *,
    run_id: str,
    round: int,  # noqa: A002 — the domain term; shadowing builtin `round` is fine locally
    spent_tokens: int,
    agents_spawned: int,
    state: Mapping[str, Any],
) -> bool:
    """Commit one round's state — the last committed round, the tree counters (so a resume
    rewinds to these, never double-counting the re-run round), and the rehydrate payload.
    Guarded on `status='running'`, so a late write to a finished/cancelled run is a no-op.
    Returns whether a running row was updated."""
    async with scoped_session(maker, ctx) as session:
        res = await session.execute(
            text(
                "UPDATE app.research_run_state"
                " SET round = :round, spent_tokens = :spent, agents_spawned = :agents,"
                "     state = CAST(:state AS jsonb), updated_at = now()"
                " WHERE run_id = :run_id AND status = 'running'"
            ),
            {
                "run_id": run_id,
                "round": round,
                "spent": spent_tokens,
                "agents": agents_spawned,
                "state": json.dumps(dict(state)),
            },
        )
        return cast(CursorResult[Any], res).rowcount > 0


async def finish(
    maker: async_sessionmaker, ctx: SessionContext, *, run_id: str, status: str
) -> bool:
    """Move a running run to a terminal status. Guarded on `status='running'` so a terminal
    state is sticky (a late finish, e.g. a completion racing a cancel, is a no-op)."""
    if status not in _TERMINAL:
        raise ValueError(f"invalid terminal status {status!r}; choose one of {list(_TERMINAL)}")
    async with scoped_session(maker, ctx) as session:
        res = await session.execute(
            text(
                "UPDATE app.research_run_state SET status = :status, updated_at = now()"
                " WHERE run_id = :run_id AND status = 'running'"
            ),
            {"run_id": run_id, "status": status},
        )
        return cast(CursorResult[Any], res).rowcount > 0


async def claim_resume(maker: async_sessionmaker, ctx: SessionContext, run_id: str) -> bool:
    """Atomically claim a running run for resume — sets `resumed_at` iff running and
    unclaimed, returns whether THIS caller won. Exactly-once across a restart, a retry, or
    two processes racing to resume the same run (the `resumed_at IS NULL` guard)."""
    async with scoped_session(maker, ctx) as session:
        res = await session.execute(
            text(
                "UPDATE app.research_run_state SET resumed_at = now()"
                " WHERE run_id = :run_id AND status = 'running' AND resumed_at IS NULL"
            ),
            {"run_id": run_id},
        )
        return cast(CursorResult[Any], res).rowcount > 0


async def load(maker: async_sessionmaker, ctx: SessionContext, run_id: str) -> RunState | None:
    """Read a run's checkpoint for rehydrate, or None if unknown (or RLS-invisible)."""
    async with scoped_session(maker, ctx) as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT id, run_id, session_id, question, status, round, ceiling_tokens,"
                        " wall_clock_deadline, spent_tokens, agents_spawned, state, resumed_at"
                        " FROM app.research_run_state WHERE run_id = :run_id"
                    ),
                    {"run_id": run_id},
                )
            )
            .mappings()
            .first()
        )
        return _row_to_state(row) if row is not None else None


def _row_to_state(row: Any) -> RunState:  # a SQLAlchemy RowMapping (str keys)
    state = row["state"]
    if isinstance(state, str):  # jsonb usually decodes to dict; be robust to a text driver
        state = json.loads(state)
    return RunState(
        id=str(row["id"]),
        run_id=row["run_id"],
        session_id=str(row["session_id"]) if row["session_id"] is not None else None,
        question=row["question"],
        status=row["status"],
        round=row["round"],
        ceiling_tokens=row["ceiling_tokens"],
        wall_clock_deadline=row["wall_clock_deadline"],
        spent_tokens=row["spent_tokens"],
        agents_spawned=row["agents_spawned"],
        state=state or {},
        resumed_at=row["resumed_at"],
    )
