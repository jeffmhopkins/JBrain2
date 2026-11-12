"""Migration 0262 against real Postgres: the GrowBot diary keeps each row once, reads a window
oldest first a page at a time, prunes what is past its week, and is owner-only; and the call
log's paged read (the brain log's) walks its window the same way.

The mandatory RLS isolation test for `growbot_journal` (CLAUDE.md rule 3).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.growbot import journal as journal_mod
from jbrain.growbot.calls import CallRecord, SqlCallLog
from jbrain.growbot.journal import ROW_MAX, DiaryBatch, SqlJournal
from tests.conftest import docker_available
from tests.integration.test_growbot_rls import NON_OWNERS
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

SYSTEM_OWNER = SessionContext(principal_kind="owner")
_PERSONA = "0b7c2f4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
_HOUR_AGO = datetime.now(UTC) - timedelta(hours=1)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    made = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(made, SYSTEM_OWNER) as session:
        await session.execute(text("DELETE FROM app.growbot_journal"))
        await session.execute(text("DELETE FROM app.growbot_calls"))
    yield made
    await engine.dispose()


def _batch(*rows: dict[str, object], sid: str = "nx-abc") -> DiaryBatch:
    return DiaryBatch(
        sid=sid,
        rows=list(rows),
        persona_id=_PERSONA,
        robot="gb-abc123",
        head={"boot": {"t": 1, "bld": "deadbeef"}},
    )


async def test_a_snapshot_resent_keeps_each_row_once(maker: async_sessionmaker) -> None:
    j = SqlJournal(maker)
    assert await j.record(SYSTEM_OWNER, _batch({"t": 1, "e": "boot"}, {"t": 2, "e": "heard"})) == 2
    # The next snapshot carries the same rows plus a new one, and a twin inside one post.
    again = _batch({"t": 1, "e": "boot"}, {"t": 2, "e": "heard"}, {"t": 3, "e": "act"})
    assert await j.record(SYSTEM_OWNER, again) == 1
    assert await j.record(SYSTEM_OWNER, _batch({"t": 4, "e": "x"}, {"t": 4, "e": "x"})) == 1
    # Same t, different body: a second event in the same millisecond is its own row.
    assert await j.record(SYSTEM_OWNER, _batch({"t": 4, "e": "y"})) == 1
    # Another diary's row at the same t and body is its own too.
    assert await j.record(SYSTEM_OWNER, _batch({"t": 1, "e": "boot"}, sid="nx-other")) == 1
    got = await j.page(OWNER, since=_HOUR_AGO)
    assert [(r.sid, r.t, r.row["e"]) for r in got] == [
        ("nx-abc", 1, "boot"),
        ("nx-abc", 2, "heard"),
        ("nx-abc", 3, "act"),
        ("nx-abc", 4, "x"),
        ("nx-abc", 4, "y"),
        ("nx-other", 1, "boot"),
    ]
    assert got[0].persona_id == _PERSONA and got[0].robot == "gb-abc123"
    assert got[0].head == {"boot": {"t": 1, "bld": "deadbeef"}}


async def test_odd_rows_are_skipped_not_the_batch(maker: async_sessionmaker) -> None:
    j = SqlJournal(maker)
    batch = _batch(
        {"e": "no time"},
        {"t": "5", "e": "text time"},
        {"t": True, "e": "bool time"},
        {"t": 6, "e": "big", "pad": "x" * ROW_MAX},
        {"t": 7.0, "e": "kept"},
    )
    assert await j.record(SYSTEM_OWNER, batch) == 1
    assert [r.t for r in await j.page(OWNER, since=_HOUR_AGO)] == [7]
    assert await j.record(SYSTEM_OWNER, _batch()) == 0


async def test_a_window_is_read_a_page_at_a_time(maker: async_sessionmaker) -> None:
    j = SqlJournal(maker)
    await j.record(SYSTEM_OWNER, _batch(*({"t": i, "e": "tick"} for i in range(5))))
    first = await j.page(OWNER, since=_HOUR_AGO, limit=2)
    rest = await j.page(OWNER, since=_HOUR_AGO, after_id=first[-1].id, limit=10)
    assert [r.t for r in first + rest] == [0, 1, 2, 3, 4]
    assert await j.page(OWNER, since=datetime.now(UTC) + timedelta(minutes=1)) == []


async def test_the_diary_is_owner_only(maker: async_sessionmaker) -> None:
    await SqlJournal(maker).record(SYSTEM_OWNER, _batch({"t": 1, "e": "boot"}))
    for ctx in NON_OWNERS:
        assert await SqlJournal(maker).page(ctx, since=_HOUR_AGO) == [], ctx
        with pytest.raises(DBAPIError):
            await SqlJournal(maker).record(ctx, _batch({"t": 2, "e": "boot"}))
    assert len(await SqlJournal(maker).page(OWNER, since=_HOUR_AGO)) == 1


async def test_rows_past_their_week_are_pruned(
    maker: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with scoped_session(maker, SYSTEM_OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.growbot_journal (sid, t, row_hash, row, created_at)"
                " VALUES ('nx-old', 1, 'h', '{}'::jsonb, now() - interval '8 days')"
            )
        )
    monkeypatch.setattr(journal_mod, "_PRUNE_EVERY", 1)
    await SqlJournal(maker).record(SYSTEM_OWNER, _batch({"t": 1, "e": "boot"}))
    async with scoped_session(maker, SYSTEM_OWNER) as session:
        sids = (await session.execute(text("SELECT sid FROM app.growbot_journal"))).scalars()
        assert list(sids) == ["nx-abc"]


async def test_the_call_logs_window_is_read_oldest_first_a_page_at_a_time(
    maker: async_sessionmaker,
) -> None:
    log = SqlCallLog(maker)
    for kind in ("llm", "stt", "tts"):
        await log.record(SYSTEM_OWNER, CallRecord(kind=kind, ok=True, total_ms=1))
    first = await log.page(OWNER, since=_HOUR_AGO, limit=2)
    rest = await log.page(OWNER, since=_HOUR_AGO, after_id=first[-1].id)
    assert [c.record.kind for c in first + rest] == ["llm", "stt", "tts"]
    assert await log.page(OWNER, since=datetime.now(UTC) + timedelta(minutes=1)) == []
    for ctx in NON_OWNERS:
        assert await log.page(ctx, since=_HOUR_AGO) == [], ctx
