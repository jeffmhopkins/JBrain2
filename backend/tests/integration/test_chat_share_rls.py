"""Migration 0238 against real Postgres: chat share links are enforced in RLS, not app code.

The security keystone. A visitor runs under `chat_share_context` — a non-owner, empty-scope
principal pinned to one share-link id. The three `*_share` policies are the ONLY grants it
earns: it reads exactly the pinned link's chat (its session row, turns and runs) and nothing
else — not another chat, not the chat's own sub-agent child, not a note, not a chat attachment
— with revocation re-checked at the data layer and no write anywhere.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.chat_shares import (
    list_shares,
    mint_share,
    read_shared_chat,
    resolve_share,
    revoke_share,
    shared_hosts,
)
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, chat_share_context, scoped_session
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
    # chat_share_links.principal_id FKs to app.principals, so the owner ctx must carry a REAL
    # owner principal — rotate a key, read it back.
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


_STEP = {
    "id": "t1",
    "name": "web_search",
    "ok": True,
    "web_sources": [
        {"url": "https://www.cnbc.com/spacex", "title": "CNBC"},
        {"url": "https://forgeglobal.com/spacex_stock/", "title": "Forge"},
    ],
}


async def _chat(maker: async_sessionmaker, owner: SessionContext, title: str) -> str:
    chat = await AgentSessionRepo(maker).create(
        owner, domain_scopes=["general", "finance"], title=title, agent="jerv"
    )
    run = await AgentRunLog(maker).start(owner, session_id=chat.id, prompt_version="v")
    await AgentTranscript(maker).record_exchange(
        owner,
        session_id=chat.id,
        run_id=run,
        user_text=f"{title} question",
        assistant_text=f"{title} answer",
        tools=[_STEP],
        reasoning="thinking it over",
    )
    return chat.id


async def _child(maker: async_sessionmaker, owner: SessionContext, parent: str) -> str:
    """A sub-agent child of `parent`: its own session row with a turn of its own."""
    child = str(uuid.uuid4())
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.agent_sessions"
                " (id, principal_id, title, status, domain_scopes, subject_ids, agent,"
                "  parent_session_id, depth)"
                " VALUES (cast(:id AS uuid), cast(:pid AS uuid), 'child', 'active',"
                "  ARRAY['general'], ARRAY[]::uuid[], 'jerv', cast(:parent AS uuid), 1)"
            ),
            {"id": child, "pid": owner.principal_id, "parent": parent},
        )
    await AgentTranscript(maker).record_answer(
        owner, session_id=child, run_id=None, assistant_text="child answer", tools=[]
    )
    return child


async def _visible(maker: async_sessionmaker, link_id: str, sql: str) -> set[str]:
    async with scoped_session(maker, chat_share_context(link_id)) as s:
        return {str(r[0]) for r in (await s.execute(text(sql))).all()}


async def test_a_link_reads_only_its_own_chat(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    a = await _chat(maker, owner, "SpaceX Pretrade")
    b = await _chat(maker, owner, "Weather")
    child = await _child(maker, owner, a)
    minted = await mint_share(maker, owner, a)
    assert minted is not None
    token, link = minted

    assert await _visible(maker, link.id, "SELECT id FROM app.agent_sessions") == {a}
    assert await _visible(maker, link.id, "SELECT DISTINCT session_id FROM app.agent_turns") == {a}
    assert await _visible(maker, link.id, "SELECT DISTINCT session_id FROM app.runs") == {a}
    # The other chat and the shared chat's own sub-agent child stay owner-only.
    assert b not in await _visible(maker, link.id, "SELECT id FROM app.agent_sessions")
    assert child not in await _visible(maker, link.id, "SELECT id FROM app.agent_sessions")

    # The read the public route makes: header + every turn, with the run's elapsed time.
    transcript = AgentTranscript(maker)
    chat = await read_shared_chat(maker, transcript, link.id, a)
    assert chat is not None
    assert chat.title == "SpaceX Pretrade"
    assert [t.content for t in chat.turns] == ["SpaceX Pretrade question", "SpaceX Pretrade answer"]
    assert chat.turns[1].reasoning == "thinking it over"
    assert chat.turns[1].elapsed_ms is not None
    # Pinned to A, asking for B by id reads nothing.
    assert await read_shared_chat(maker, transcript, link.id, b) is None

    assert await resolve_share(maker, token) == (link.id, a)
    assert await shared_hosts(maker, link.id) == {"www.cnbc.com", "forgeglobal.com"}


async def test_an_unpinned_or_bogus_context_reads_nothing(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    a = await _chat(maker, owner, "Desk panel")
    assert await mint_share(maker, owner, a) is not None
    for pin in ("", "not-a-uuid", str(uuid.uuid4())):
        assert await _visible(maker, pin, "SELECT id FROM app.agent_sessions") == set()
        assert await _visible(maker, pin, "SELECT id FROM app.agent_turns") == set()
        assert await _visible(maker, pin, "SELECT id FROM app.runs") == set()


async def test_the_visitor_reads_no_notes_or_attachments(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    a = await _chat(maker, owner, "News")
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (gen_random_uuid(), :cid, 'finance', 'allocation price')"
            ),
            {"cid": uuid.uuid4().hex},
        )
    minted = await mint_share(maker, owner, a)
    assert minted is not None
    _, link = minted
    assert await _visible(maker, link.id, "SELECT id FROM app.notes") == set()
    assert await _visible(maker, link.id, "SELECT id FROM app.turn_attachments") == set()
    assert await _visible(maker, link.id, "SELECT id FROM app.generated_images") == set()


async def test_revocation_fails_the_read_closed(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    a = await _chat(maker, owner, "Email overview")
    minted = await mint_share(maker, owner, a)
    assert minted is not None
    token, link = minted
    assert [s.id for s in await list_shares(maker, owner, a)] == [link.id]

    assert await revoke_share(maker, owner, link.id) is True
    assert await revoke_share(maker, owner, link.id) is False  # already revoked
    assert await revoke_share(maker, owner, "not-a-uuid") is False
    assert await list_shares(maker, owner, a) == []
    assert await resolve_share(maker, token) is None
    assert await _visible(maker, link.id, "SELECT id FROM app.agent_sessions") == set()
    assert await _visible(maker, link.id, "SELECT id FROM app.agent_turns") == set()


async def test_the_visitor_cannot_write(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    a = await _chat(maker, owner, "This week in space flight")
    minted = await mint_share(maker, owner, a)
    assert minted is not None
    _, link = minted
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, chat_share_context(link.id)) as s:
            await s.execute(
                text(
                    "INSERT INTO app.agent_turns (id, session_id, role, content)"
                    " VALUES (gen_random_uuid(), cast(:sid AS uuid), 'user', 'injected')"
                ),
                {"sid": a},
            )
    async with scoped_session(maker, chat_share_context(link.id)) as s:
        updated = await s.execute(text("UPDATE app.agent_sessions SET title = 'x' RETURNING id"))
        assert updated.all() == []
        bumped = await s.execute(
            text("UPDATE app.chat_share_links SET revoked_at = NULL RETURNING id")
        )
        assert bumped.all() == []


async def test_mint_refuses_a_missing_chat_and_counts_views(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    assert await mint_share(maker, owner, str(uuid.uuid4())) is None
    assert await mint_share(maker, owner, "nope") is None
    assert await list_shares(maker, owner, "nope") == []
    assert await resolve_share(maker, "") is None
    assert await resolve_share(maker, "unknown-token") is None

    a = await _chat(maker, owner, "Weather")
    minted = await mint_share(maker, owner, a)
    assert minted is not None
    token, link = minted
    await resolve_share(maker, token, count_view=True)
    await resolve_share(maker, token, count_view=True)
    await resolve_share(maker, token)  # a favicon lookup is not a view
    (listed,) = await list_shares(maker, owner, a)
    assert listed.view_count == 2
    assert listed.last_viewed_at is not None


async def test_a_deleted_chat_takes_its_links(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    a = await _chat(maker, owner, "Desk panel")
    minted = await mint_share(maker, owner, a)
    assert minted is not None
    token, _ = minted
    await AgentSessionRepo(maker).delete(owner, a)
    assert await resolve_share(maker, token) is None


async def test_the_chats_list_flags_shared_chats(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    a = await _chat(maker, owner, "Shared one")
    b = await _chat(maker, owner, "Private one")
    minted = await mint_share(maker, owner, a)
    assert minted is not None
    flags = {s.id: s.shared for s in await AgentSessionRepo(maker).list(owner)}
    assert flags[a] is True and flags[b] is False
    await revoke_share(maker, owner, minted[1].id)
    flags = {s.id: s.shared for s in await AgentSessionRepo(maker).list(owner)}
    assert flags[a] is False
