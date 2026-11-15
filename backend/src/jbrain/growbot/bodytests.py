"""GrowBot body tests (migration 0274): a guided real-robot trial's runs and logs.

The creature's phone (growbot/patches/growbot-bodytest.js) posts a run's log in numbered chunks
through the sidecar. The first chunk makes the run; each one replaces its status and `summary`
(the run's latest state, which the Body tab mirrors) and keeps its rows once by `(run, seq)`, so
a chunk the phone resends is not kept twice. A run is read back whole, its chunks in order, for
review from the owner's PWA or the debug API.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

# The page posts every 2 s or 400 rows; these bound a page gone wrong.
ROWS_MAX: Final = 1000
CHUNK_MAX: Final = 512 * 1024
SUMMARY_MAX: Final = 16 * 1024
SEQ_MAX: Final = 100_000
KEEP_RUNS: Final = 50
STATUSES: Final = frozenset({"running", "done", "stopped", "failed"})


@dataclass(frozen=True)
class Chunk:
    run: str
    robot: str
    seq: int
    status: str
    summary: dict[str, Any]
    rows: list[dict[str, Any]]
    persona_id: str | None = None
    skills: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Run:
    id: int
    run: str
    robot: str
    persona_id: str | None
    started_at: datetime
    updated_at: datetime
    status: str
    skills: dict[str, Any]
    summary: dict[str, Any]
    rows: int


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class BodyTests(Protocol):
    async def record(self, ctx: SessionContext, chunk: Chunk) -> int | None: ...

    async def runs(self, ctx: SessionContext, *, robot: str = "", limit: int = 10) -> list[Run]: ...

    async def run(self, ctx: SessionContext, number: int) -> Run | None: ...

    async def rows(self, ctx: SessionContext, number: int) -> list[dict[str, Any]]: ...


_COLS = (
    "id, run, robot, persona_id::text AS persona_id, started_at, updated_at, status, skills,"
    " summary, row_count"
)


def _run(r: Any) -> Run:
    return Run(
        id=r["id"],
        run=r["run"],
        robot=r["robot"],
        persona_id=r["persona_id"],
        started_at=r["started_at"],
        updated_at=r["updated_at"],
        status=r["status"],
        skills=r["skills"],
        summary=r["summary"],
        rows=r["row_count"],
    )


class SqlBodyTests:
    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker

    async def record(self, ctx: SessionContext, chunk: Chunk) -> int | None:
        """Keep one chunk; returns the run's number, or None when it isn't one to keep (a chunk
        too big, a run of another robot under the same id, or a finished run's straggler)."""
        rows = [r for r in chunk.rows[:ROWS_MAX] if isinstance(r, dict)]
        body = _dump(rows)
        summary = _dump(chunk.summary)
        if len(body) > CHUNK_MAX or chunk.status not in STATUSES or not 0 <= chunk.seq < SEQ_MAX:
            return None
        if len(summary) > SUMMARY_MAX:
            summary = "{}"
        async with scoped_session(self._maker, ctx) as session:
            got = await session.execute(
                text(
                    "INSERT INTO app.growbot_bodytests (run, robot, persona_id, status, skills,"
                    " summary) VALUES (:run, :robot, CAST(:persona AS uuid), :status,"
                    " CAST(:skills AS jsonb), CAST(:summary AS jsonb))"
                    " ON CONFLICT (run) DO UPDATE SET updated_at = now(),"
                    " status = EXCLUDED.status, summary = EXCLUDED.summary"
                    " WHERE app.growbot_bodytests.robot = EXCLUDED.robot"
                    " AND app.growbot_bodytests.status = 'running'"
                    " RETURNING id, (xmax = 0) AS made"
                ),
                {
                    "run": chunk.run,
                    "robot": chunk.robot,
                    "persona": chunk.persona_id,
                    "status": chunk.status,
                    "skills": _dump(chunk.skills),
                    "summary": summary,
                },
            )
            row = got.mappings().first()
            if row is None:
                return None
            number = int(row["id"])
            kept = await session.execute(
                text(
                    "INSERT INTO app.growbot_bodytest_chunks (run_id, seq, body)"
                    " VALUES (:id, :seq, CAST(:rows AS jsonb))"
                    " ON CONFLICT (run_id, seq) DO NOTHING RETURNING seq"
                ),
                {"id": number, "seq": chunk.seq, "rows": body},
            )
            if kept.first() is not None and rows:
                await session.execute(
                    text(
                        "UPDATE app.growbot_bodytests SET row_count = row_count + :n WHERE id = :id"
                    ),
                    {"n": len(rows), "id": number},
                )
            if row["made"]:
                await session.execute(
                    text(
                        "DELETE FROM app.growbot_bodytests WHERE id NOT IN"
                        " (SELECT id FROM app.growbot_bodytests ORDER BY id DESC LIMIT :keep)"
                    ),
                    {"keep": KEEP_RUNS},
                )
            return number

    async def runs(self, ctx: SessionContext, *, robot: str = "", limit: int = 10) -> list[Run]:
        """The newest runs first, of one robot when named."""
        async with scoped_session(self._maker, ctx) as session:
            got = await session.execute(
                text(
                    f"SELECT {_COLS} FROM app.growbot_bodytests"
                    " WHERE (:robot = '' OR robot = :robot) ORDER BY id DESC LIMIT :limit"
                ),
                {"robot": robot, "limit": max(1, min(limit, KEEP_RUNS))},
            )
            return [_run(r) for r in got.mappings()]

    async def run(self, ctx: SessionContext, number: int) -> Run | None:
        async with scoped_session(self._maker, ctx) as session:
            got = await session.execute(
                text(f"SELECT {_COLS} FROM app.growbot_bodytests WHERE id = :id"), {"id": number}
            )
            r = got.mappings().first()
            return _run(r) if r is not None else None

    async def rows(self, ctx: SessionContext, number: int) -> list[dict[str, Any]]:
        """Every row of a run's log, in the order the phone logged them."""
        async with scoped_session(self._maker, ctx) as session:
            got = await session.execute(
                text(
                    "SELECT body FROM app.growbot_bodytest_chunks WHERE run_id = :id ORDER BY seq"
                ),
                {"id": number},
            )
            out: list[dict[str, Any]] = []
            for (chunk,) in got:
                out.extend(chunk)
            return out
