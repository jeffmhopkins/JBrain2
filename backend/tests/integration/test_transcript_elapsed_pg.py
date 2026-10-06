"""`AgentTranscript.load` serves each assistant turn's total wall time (`elapsed_ms`) from
its run's start on real Postgres — one RLS-scoped join, null for a run-less turn, and the
owner's runs invisible to another principal."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _backdate_run(maker: async_sessionmaker, run_id: str, *, seconds: int) -> None:
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "UPDATE app.runs SET started_at = now() - make_interval(secs => :s) WHERE id = :id"
            ),
            {"s": seconds, "id": uuid.UUID(run_id)},
        )


async def test_load_serves_assistant_elapsed_ms_from_the_run_start(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner(maker)
    chat = await AgentSessionRepo(maker).create(
        owner, domain_scopes=["general"], title="t", agent="jerv"
    )
    runlog = AgentRunLog(maker)
    transcript = AgentTranscript(maker)

    timed = await runlog.start(owner, session_id=chat.id, prompt_version="v")
    # The turn waited (queued browse, slow model): the run started 112s before it settled.
    await _backdate_run(maker, timed, seconds=112)
    await transcript.record_exchange(
        owner, session_id=chat.id, run_id=timed, user_text="q1", assistant_text="a1", tools=[]
    )
    await transcript.record_exchange(
        owner, session_id=chat.id, run_id=None, user_text="q2", assistant_text="a2", tools=[]
    )
    stale = await runlog.start(owner, session_id=chat.id, prompt_version="v")
    await _backdate_run(maker, stale, seconds=7 * 3600)
    await transcript.record_answer(
        owner, session_id=chat.id, run_id=stale, assistant_text="a3", tools=[]
    )

    turns = await transcript.load(owner, chat.id)
    assert [t.role for t in turns] == ["user", "assistant", "user", "assistant", "assistant"]
    assert turns[0].elapsed_ms is None  # user turns never carry a span
    assert turns[1].elapsed_ms is not None and 112_000 <= turns[1].elapsed_ms < 120_000
    assert turns[3].elapsed_ms is None  # run-less turn
    assert turns[4].elapsed_ms is None  # > 6h is a mismatch, not a wall time

    # RLS-scoped: another principal reads none of the owner's transcript or runs.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await transcript.load(token, chat.id) == []
