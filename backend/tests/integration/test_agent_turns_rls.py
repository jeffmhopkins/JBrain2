"""Migration 0020 against real Postgres: a session's transcript persists in order
(user then assistant, with the assistant turn's tool sources) and the table is
owner-only (CLAUDE.md rule 3)."""

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


async def test_transcript_persists_in_order_and_is_owner_only(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sessions = AgentSessionRepo(maker)
    info = await sessions.create(owner, domain_scopes=["general"], title="ask")
    run_id = await AgentRunLog(maker).start(owner, session_id=info.id, prompt_version="v1")

    store = AgentTranscript(maker)
    await store.record_exchange(
        owner,
        session_id=info.id,
        run_id=run_id,
        user_text="when was I born?",
        assistant_text="March 19, 1986.",
        tools=[
            {
                "id": "c1",
                "name": "search",
                "ok": True,
                "sources": [{"note_id": "n1", "domain": "general", "snippet": "born"}],
            }
        ],
    )

    turns = await store.load(owner, info.id)
    assert [(t.role, t.content) for t in turns] == [
        ("user", "when was I born?"),
        ("assistant", "March 19, 1986."),
    ]
    # The assistant turn carries the Worked-block sources.
    assert turns[1].tools[0]["name"] == "search"
    assert turns[1].tools[0]["sources"][0]["note_id"] == "n1"

    # Owner-only: a non-owner principal sees no turns and loads nothing.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    async with scoped_session(maker, token) as session:
        assert (await session.execute(text("SELECT count(*) FROM app.agent_turns"))).scalar() == 0
    assert await store.load(token, info.id) == []
