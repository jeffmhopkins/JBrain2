"""`AgentTranscript.tool_names` on real Postgres: the tool history the disk prompt cache reads
to keep a chat in which a location, mail or records tool ran off disk (FLASH_NEXT F4c), read
RLS-scoped — another principal sees none of it."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

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


async def test_tool_names_are_every_tool_any_assistant_turn_ran(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner(maker)
    sessions = AgentSessionRepo(maker)
    transcript = AgentTranscript(maker)
    chat = await sessions.create(owner, domain_scopes=["general"], title="t", agent="jerv")
    other = await sessions.create(owner, domain_scopes=["general"], title="o", agent="jerv")
    await transcript.record_exchange(
        owner,
        session_id=chat.id,
        run_id=None,
        user_text="where am I",
        assistant_text="home",
        tools=[{"name": "current_location", "ok": True}, {"name": "web_search", "ok": True}],
    )
    await transcript.record_exchange(
        owner,
        session_id=chat.id,
        run_id=None,
        user_text="and 2+2",
        assistant_text="4",
        tools=[{"name": "calculate", "ok": True}, {"bogus": 1}],
    )
    await transcript.record_exchange(
        owner,
        session_id=other.id,
        run_id=None,
        user_text="hi",
        assistant_text="hello",
        tools=[],
    )
    assert await transcript.tool_names(owner, chat.id) == {
        "current_location",
        "web_search",
        "calculate",
    }
    assert await transcript.tool_names(owner, other.id) == set()
    # RLS-scoped: a non-owner principal (a capability token, even in the session's own domain)
    # reads nothing of the owner's transcript.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await transcript.tool_names(token, chat.id) == set()
