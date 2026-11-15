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
# A run's own bound: a chunk every 2 s is 50 minutes, ~20 samples a second about 40 minutes,
# so a page stuck posting stops growing its run (and a whole run reads back in one response).
SEQ_MAX: Final = 1500
RUN_ROWS_MAX: Final = 60_000
# Per robot, so one phone's runs never evict another robot's.
KEEP_RUNS: Final = 50
# A listing spans robots, so it isn't bounded by one robot's keep.
LIST_MAX: Final = 200
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


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _dump(value: Any) -> str:
    # jsonb takes neither a NUL nor a lone surrogate (which can't even be encoded), so NULs are
    # dropped from the strings themselves (not the dumped text, where a literal "\\u0000" typed
    # into a transcript must survive) and a surrogate becomes "?": one odd string never costs a
    # chunk.
    text = json.dumps(_clean(value), separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8", "replace").decode("utf-8")


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
        # A run that ends past its bounds still gets its ending (status and verdicts), only not
        # its rows, or it would read as running forever.
        final = chunk.status != "running"
        if chunk.status not in STATUSES or chunk.seq < 0 or (chunk.seq >= SEQ_MAX and not final):
            return None
        rows = [r for r in chunk.rows[:ROWS_MAX] if isinstance(r, dict)]
        if chunk.seq >= SEQ_MAX:
            rows = []
        body = _dump(rows)
        summary = _dump(chunk.summary)
        if len(body) > CHUNK_MAX:
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
                    " AND (app.growbot_bodytests.row_count < :cap"
                    " OR EXCLUDED.status <> 'running')"
                    " RETURNING id, (xmax = 0) AS made, row_count"
                ),
                {
                    "run": chunk.run,
                    "robot": chunk.robot,
                    "persona": chunk.persona_id,
                    "status": chunk.status,
                    "skills": _dump(chunk.skills),
                    "summary": summary,
                    "cap": RUN_ROWS_MAX,
                },
            )
            row = got.mappings().first()
            if row is None:
                return None
            number = int(row["id"])
            if chunk.seq >= SEQ_MAX or row["row_count"] >= RUN_ROWS_MAX:
                return number
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
                        "DELETE FROM app.growbot_bodytests WHERE robot = :robot AND id NOT IN"
                        " (SELECT id FROM app.growbot_bodytests WHERE robot = :robot"
                        " ORDER BY id DESC LIMIT :keep)"
                    ),
                    {"robot": chunk.robot, "keep": KEEP_RUNS},
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
                {"robot": robot, "limit": max(1, min(limit, LIST_MAX))},
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
