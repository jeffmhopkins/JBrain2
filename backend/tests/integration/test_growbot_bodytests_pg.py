"""Migration 0274 against real Postgres: a body test's chunks make one run, kept once each, its
status and summary following the newest chunk until it ends; a run reads back whole and in
order; only the newest runs are kept; and both tables are owner-only.

The mandatory RLS isolation test for `growbot_bodytests` and `growbot_bodytest_chunks`
(CLAUDE.md rule 3).
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.growbot import bodytests as bodytests_mod
from jbrain.growbot.bodytests import Chunk, SqlBodyTests
from tests.conftest import docker_available
from tests.integration.test_growbot_rls import NON_OWNERS
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

SYSTEM_OWNER = SessionContext(principal_kind="owner")
_PERSONA = "0b7c2f4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
_TABLES = ("growbot_bodytest_chunks", "growbot_bodytests")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    made = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(made, SYSTEM_OWNER) as session:
        await session.execute(text("DELETE FROM app.growbot_bodytests"))
    yield made
    await engine.dispose()


def _chunk(seq: int, *rows: dict[str, Any], run: str = "bt-a1-b2", **over: Any) -> Chunk:
    base: dict[str, Any] = {
        "run": run,
        "robot": "gb-abc123",
        "seq": seq,
        "status": "running",
        "summary": {"step": f"s{seq}"},
        "rows": list(rows),
        "persona_id": _PERSONA,
        "skills": {"look": "x"},
    }
    base.update(over)
    return Chunk(**base)


async def test_chunks_make_one_run_read_back_in_order(maker: async_sessionmaker) -> None:
    bt = SqlBodyTests(maker)
    first = await bt.record(SYSTEM_OWNER, _chunk(0, {"t": 1, "k": "e"}, {"t": 2, "k": "s"}))
    assert first is not None
    # Out of order and resent: each chunk is kept once, and read back by its seq.
    assert await bt.record(SYSTEM_OWNER, _chunk(2, {"t": 5, "k": "s"})) == first
    assert await bt.record(SYSTEM_OWNER, _chunk(1, {"t": 3, "k": "s"}, {"t": 4})) == first
    assert await bt.record(SYSTEM_OWNER, _chunk(1, {"t": 3, "k": "s"}, {"t": 4})) == first
    assert [r["t"] for r in await bt.rows(OWNER, first)] == [1, 2, 3, 4, 5]
    run = await bt.run(OWNER, first)
    assert run is not None
    assert (run.run, run.robot, run.persona_id, run.rows) == ("bt-a1-b2", "gb-abc123", _PERSONA, 5)
    assert run.status == "running" and run.summary == {"step": "s1"}
    assert run.skills == {"look": "x"}


async def test_a_finished_run_takes_no_more(maker: async_sessionmaker) -> None:
    bt = SqlBodyTests(maker)
    number = await bt.record(SYSTEM_OWNER, _chunk(0, {"t": 1}))
    final = _chunk(1, {"t": 2}, status="done", summary={"results": {"turn": {"v": "ok"}}})
    assert await bt.record(SYSTEM_OWNER, final) == number
    assert await bt.record(SYSTEM_OWNER, _chunk(2, {"t": 3})) is None
    run = await bt.run(OWNER, number or 0)
    assert run is not None and run.status == "done" and run.summary["results"]["turn"]["v"] == "ok"
    assert [r["t"] for r in await bt.rows(OWNER, number or 0)] == [1, 2]


async def test_a_run_id_is_never_taken_over_by_another_robot(maker: async_sessionmaker) -> None:
    bt = SqlBodyTests(maker)
    number = await bt.record(SYSTEM_OWNER, _chunk(0, {"t": 1}))
    assert await bt.record(SYSTEM_OWNER, _chunk(1, {"t": 2}, robot="gb-other")) is None
    assert [r["t"] for r in await bt.rows(OWNER, number or 0)] == [1]


async def test_odd_chunks_are_refused(maker: async_sessionmaker) -> None:
    bt = SqlBodyTests(maker)
    assert await bt.record(SYSTEM_OWNER, _chunk(0, status="lost")) is None
    assert await bt.record(SYSTEM_OWNER, _chunk(-1)) is None
    huge = {"t": 1, "x": "y" * (bodytests_mod.CHUNK_MAX + 1)}
    assert await bt.record(SYSTEM_OWNER, _chunk(0, huge)) is None
    assert await bt.runs(OWNER) == []
    # A summary past its bound is kept empty, never refusing the rows with it.
    number = await bt.record(
        SYSTEM_OWNER, _chunk(0, {"t": 1}, summary={"x": "y" * bodytests_mod.SUMMARY_MAX})
    )
    run = await bt.run(OWNER, number or 0)
    assert run is not None and run.summary == {}


async def test_runs_list_newest_first_and_only_the_newest_are_kept(
    maker: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bodytests_mod, "KEEP_RUNS", 3)
    bt = SqlBodyTests(maker)
    made = []
    for i in range(5):
        robot = "gb-abc123" if i % 2 == 0 else "gb-other"
        made.append(
            await bt.record(SYSTEM_OWNER, _chunk(0, {"t": i}, run=f"bt-r{i}-x", robot=robot))
        )
    got = await bt.runs(OWNER)
    assert [r.id for r in got] == made[:1:-1]
    assert [r.run for r in await bt.runs(OWNER, robot="gb-abc123")] == ["bt-r4-x", "bt-r2-x"]
    assert len(await bt.runs(OWNER, limit=1)) == 1
    # A pruned run's chunks go with it.
    assert await bt.rows(OWNER, made[0] or 0) == []


async def test_both_tables_are_owner_only(maker: async_sessionmaker) -> None:
    bt = SqlBodyTests(maker)
    number = await bt.record(SYSTEM_OWNER, _chunk(0, {"t": 1}))
    for ctx in NON_OWNERS:
        assert await bt.runs(ctx) == [], ctx
        assert await bt.run(ctx, number or 0) is None, ctx
        assert await bt.rows(ctx, number or 0) == [], ctx
        async with scoped_session(maker, ctx) as session:
            for table in _TABLES:
                count = await session.execute(text(f"SELECT count(*) FROM app.{table}"))
                assert count.scalar() == 0, (table, ctx)


@pytest.mark.parametrize("ctx", NON_OWNERS)
async def test_a_non_owner_cannot_write_a_run(
    maker: async_sessionmaker, ctx: SessionContext
) -> None:
    with pytest.raises(DBAPIError):
        await SqlBodyTests(maker).record(ctx, _chunk(0, {"t": 1}, run="bt-evil-x"))
    async with scoped_session(maker, SYSTEM_OWNER) as session:
        count = await session.execute(text("SELECT count(*) FROM app.growbot_bodytests"))
        assert count.scalar() == 0
