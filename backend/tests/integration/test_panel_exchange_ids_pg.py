"""A desk-panel turn names its own question and answer to Listen (firmware-c6 0.0.21), found by
its run — never the newest pair, which an exchange the PWA finished in the same chat can be
(review of #1736)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.api import panel_agent
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
    async with scoped_session(maker, OWNER) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _exchange(maker: async_sessionmaker, owner: SessionContext, sid: str, q: str) -> str:
    run_id = await AgentRunLog(maker).start(owner, session_id=sid, prompt_version="v")
    await AgentTranscript(maker).record_exchange(
        owner, session_id=sid, run_id=run_id, user_text=q, assistant_text=f"re {q}", tools=[]
    )
    return run_id


async def _seqs(maker: async_sessionmaker, owner: SessionContext, run_id: str) -> list[int]:
    async with scoped_session(maker, owner) as s:
        rows = await s.execute(
            text("SELECT seq FROM app.agent_turns WHERE run_id = CAST(:r AS uuid) ORDER BY seq"),
            {"r": run_id},
        )
        return [int(r[0]) for r in rows]


async def test_the_panel_s_turn_is_named_even_after_a_pwa_exchange(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner(maker)
    sid = (await AgentSessionRepo(maker).create(owner, domain_scopes=[], title="desk")).id
    panel_run = await _exchange(maker, owner, sid, "from the panel")
    await _exchange(maker, owner, sid, "from the PWA")  # finished meanwhile, in the same chat
    started = panel_agent.Started(owner, sid, 0.0, run_id=panel_run)
    got = await panel_agent.exchange_ids(maker, started)
    assert got == tuple(await _seqs(maker, owner, panel_run))


async def test_a_turn_without_a_run_or_an_answer_names_nothing(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner(maker)
    sid = (await AgentSessionRepo(maker).create(owner, domain_scopes=[], title="desk")).id
    assert await panel_agent.exchange_ids(maker, panel_agent.Started(owner, sid, 0.0)) is None
    run_id = await AgentRunLog(maker).start(owner, session_id=sid, prompt_version="v")
    started = panel_agent.Started(owner, sid, 0.0, run_id=run_id)
    assert await panel_agent.exchange_ids(maker, started) is None
