"""Migration 0260 against real Postgres: the GrowBot call log is owner-only, round-trips a
row, filters by time and kind, and prunes what is past its week.

The mandatory RLS isolation test for `growbot_calls` (CLAUDE.md rule 3).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.growbot import calls as calls_mod
from jbrain.growbot.calls import CallRecord, SqlCallLog
from tests.conftest import docker_available
from tests.integration.test_growbot_rls import NON_OWNERS
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

SYSTEM_OWNER = SessionContext(principal_kind="owner")
_PERSONA = "0b7c2f4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    made = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(made, SYSTEM_OWNER) as session:
        await session.execute(text("DELETE FROM app.growbot_calls"))
    yield made
    await engine.dispose()


def _rec(kind: str = "llm", **kw: object) -> CallRecord:
    base: dict[str, object] = {
        "kind": kind,
        "ok": True,
        "total_ms": 1234,
        "persona_id": _PERSONA,
        "robot": "gb-abc123",
        "model": "qwen3-vl",
        "request": {"messages": [{"role": "user", "content": [{"type": "image"}]}]},
        "reply": "I see a cat",
        "reasoning": "hm",
        "images": 1,
        "image_bytes": 40_000,
        "prompt_tokens": 900,
        "cached_tokens": 600,
        "completion_tokens": 4,
        "timings": {"lock_ms": 3, "engine_prefill_ms": 812.4},
    }
    base.update(kw)
    return CallRecord(**base)  # type: ignore[arg-type]


async def test_a_row_round_trips(maker: async_sessionmaker) -> None:
    log = SqlCallLog(maker)
    await log.record(SYSTEM_OWNER, _rec())
    await log.record(SYSTEM_OWNER, _rec("stt", persona_id=None, images=0, prompt_tokens=None))
    got = await log.recent(OWNER)
    assert [c.record.kind for c in got] == ["stt", "llm"]
    assert got[1].record == _rec()
    assert got[0].record.persona_id is None and got[0].record.prompt_tokens is None
    assert [c.record.kind for c in await log.recent(OWNER, kind="llm")] == ["llm"]
    assert await log.recent(OWNER, since=datetime.now(UTC) + timedelta(minutes=1)) == []
    assert len(await log.recent(OWNER, limit=1)) == 1


async def test_the_log_is_owner_only(maker: async_sessionmaker) -> None:
    await SqlCallLog(maker).record(SYSTEM_OWNER, _rec())
    for ctx in NON_OWNERS:
        assert await SqlCallLog(maker).recent(ctx) == [], ctx
        with pytest.raises(DBAPIError):
            await SqlCallLog(maker).record(ctx, _rec())
    assert len(await SqlCallLog(maker).recent(OWNER)) == 1


async def test_an_unknown_kind_is_refused(maker: async_sessionmaker) -> None:
    with pytest.raises(ValueError, match="kind"):
        await SqlCallLog(maker).record(SYSTEM_OWNER, _rec("video"))


async def test_rows_past_their_week_are_pruned(
    maker: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SqlCallLog(maker)
    # The app may not UPDATE the log, so the old row is written already old.
    async with scoped_session(maker, SYSTEM_OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.growbot_calls (kind, ok, total_ms, created_at)"
                " VALUES ('llm', true, 1, now() - interval '8 days')"
            )
        )
    monkeypatch.setattr(calls_mod, "_PRUNE_EVERY", 1)
    await log.record(SYSTEM_OWNER, _rec("tts"))
    assert [c.record.kind for c in await log.recent(OWNER)] == ["tts"]
