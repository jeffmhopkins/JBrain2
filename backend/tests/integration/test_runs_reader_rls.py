"""The RunLogReader against real Postgres: it reads the owner's run log with its
step tree, and (CLAUDE.md rule 3) a non-owner session reads an empty log even
through the reader — the RLS firewall, not the API, is the enforcement point."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.runlog import AgentRunLog, RunLogReader
from jbrain.agent.session import AgentSessionRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

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
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _seed_run(maker: async_sessionmaker, owner: SessionContext) -> str:
    sessions = AgentSessionRepo(maker)
    info = await sessions.create(owner, domain_scopes=["general"], title="ask")
    log = AgentRunLog(maker)
    run_id = await log.start(owner, session_id=info.id, prompt_version="agent-system-v1")
    await log.step(owner, run_id, idx=0, kind="model", name="converse", ok=True, cost_tokens=15)
    await log.step(owner, run_id, idx=1, kind="tool", name="search", ok=False, cost_tokens=0)
    await log.finish(
        owner, run_id, status="error", stop_reason="step_error", step_count=2, cost_tokens=15
    )
    return run_id


async def test_reader_lists_and_loads_for_owner(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    run_id = await _seed_run(maker, owner)
    reader = RunLogReader(maker)

    listed = await reader.list_recent(owner)
    assert [r.id for r in listed] == [run_id]
    summary = listed[0]
    assert summary.status == "error"
    assert summary.duration_ms is not None  # finished run has an honest duration
    assert summary.last_error == "search"  # first not-ok step's name

    detail = await reader.load(owner, run_id)
    assert detail is not None
    assert detail.stop_reason == "step_error"
    assert [(s.idx, s.ok) for s in detail.steps] == [(0, True), (1, False)]
    assert detail.steps[1].error == "search"


async def test_reader_is_owner_only(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    run_id = await _seed_run(maker, owner)
    reader = RunLogReader(maker)

    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await reader.list_recent(token) == []
    assert await reader.load(token, run_id) is None


async def test_reader_bad_uuid_is_none(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    reader = RunLogReader(maker)
    assert await reader.load(owner, "not-a-uuid") is None
