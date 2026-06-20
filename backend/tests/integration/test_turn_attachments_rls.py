"""Chat-turn attachments against real Postgres: RLS isolation per CLAUDE.md rule 3.

A turn attachment carries a `domain_code` firewall (the same has_domain_scope policy
as note attachments): a health-scoped file is visible only to a health-scoped read,
and a scoped principal cannot insert an out-of-scope domain_code.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.attachments import TurnAttachmentRepo, domain_for_session
from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def sessions(maker: async_sessionmaker) -> AgentSessionRepo:
    return AgentSessionRepo(maker)


@pytest.fixture
def repo(maker: async_sessionmaker, sessions: AgentSessionRepo) -> TurnAttachmentRepo:
    return TurnAttachmentRepo(maker, sessions)


async def _owner_principal(maker: async_sessionmaker) -> str:
    """A real owner principal id — agent_sessions FK requires one, so the synthetic
    OWNER.principal_id from test_rls won't do."""
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


async def _session(
    sessions: AgentSessionRepo, owner: SessionContext, scopes: tuple[str, ...]
) -> str:
    info = await sessions.create(owner, domain_scopes=list(scopes))
    return info.id


async def test_turn_attachment_domain_firewall(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")  # full owner, all scopes
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    att = await repo.add(
        health,
        session_id,
        sha256="aa" * 32,
        filename="scan.png",
        media_type="image/png",
        size_bytes=3,
        domain_code="health",
    )

    # Visible inside the health scope; invisible to general-only and to an unscoped read.
    assert await repo.get(health, att.id) is not None
    assert await repo.get(general, att.id) is None
    assert await repo.get(UNSCOPED, att.id) is None
    # The owner (full scope) still sees it.
    assert await repo.get(owner, att.id) is not None


async def test_scoped_principal_cannot_insert_out_of_scope_domain(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("general",))
    # A general-only session physically cannot stamp a health-scoped attachment:
    # the WITH CHECK clause rejects the insert.
    with pytest.raises(ProgrammingError):
        await repo.add(
            general,
            session_id,
            sha256="bb" * 32,
            filename="x.pdf",
            media_type="application/pdf",
            size_bytes=1,
            domain_code="health",
        )


async def test_remove_respects_firewall(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    att = await repo.add(
        health,
        session_id,
        sha256="cc" * 32,
        filename="scan.png",
        media_type="image/png",
        size_bytes=2,
        domain_code="health",
    )
    # Out-of-scope delete reads as missing; in-scope delete returns the id.
    assert await repo.remove(general, att.id) is None
    assert await repo.remove(health, att.id) == att.id
    assert await repo.get(owner, att.id) is None


async def test_list_for_session_only_returns_in_scope_rows(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    await repo.add(
        health,
        session_id,
        sha256="dd" * 32,
        filename="a.png",
        media_type="image/png",
        size_bytes=1,
        domain_code="health",
    )
    assert len(await repo.list_for_session(health, session_id)) == 1
    # A general-only read of the same session sees no health-scoped file.
    assert await repo.list_for_session(general, session_id) == []


async def test_bind_to_turn_and_transcript_replay(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    # A user turn's attachments bind to its AgentTurn row and replay on load.
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    health = read_context(pid, ("health",))
    session_id = await _session(sessions, owner, ("health",))
    att = await repo.add(
        health,
        session_id,
        sha256="ee" * 32,
        filename="scan.png",
        media_type="image/png",
        size_bytes=4,
        domain_code="health",
    )
    transcript = AgentTranscript(maker, repo)
    user_turn_id = await transcript.record_exchange(
        owner,
        session_id=session_id,
        run_id=None,
        user_text="what is this?",
        assistant_text="a scan",
        tools=[],
    )
    await repo.bind_to_turn(health, [att.id], user_turn_id)

    # list_for_turns returns the bound row keyed by its user turn id.
    by_turn = await repo.list_for_turns(health, [user_turn_id])
    assert [a.id for a in by_turn[user_turn_id]] == [att.id]

    # The transcript replays the file on the USER turn; the assistant turn carries none.
    turns = await transcript.load(owner, session_id)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert [a.filename for a in turns[0].attachments] == ["scan.png"]
    assert turns[1].attachments == []


async def test_turn_cannot_reference_an_out_of_scope_attachment(
    repo: TurnAttachmentRepo, sessions: AgentSessionRepo, maker: async_sessionmaker
) -> None:
    # An attachment uploaded under one firewall cannot be bound to a turn through a
    # session narrowed to a DIFFERENT scope: RLS makes the row invisible to the bind,
    # so the UPDATE matches nothing and the turn never references the foreign file.
    pid = await _owner_principal(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    health = read_context(pid, ("health",))
    general = read_context(pid, ("general",))
    session_id = await _session(sessions, owner, ("health",))
    att = await repo.add(
        health,
        session_id,
        sha256="ff" * 32,
        filename="labs.png",
        media_type="image/png",
        size_bytes=4,
        domain_code="health",
    )
    transcript = AgentTranscript(maker, repo)
    user_turn_id = await transcript.record_exchange(
        owner,
        session_id=session_id,
        run_id=None,
        user_text="smuggle?",
        assistant_text="no",
        tools=[],
    )
    # A general-scoped bind can't see the health-scoped row, so nothing is bound.
    await repo.bind_to_turn(general, [att.id], user_turn_id)
    assert await repo.list_for_turns(health, [user_turn_id]) == {}
    # The health-scoped bind (the legitimate one) does attach it.
    await repo.bind_to_turn(health, [att.id], user_turn_id)
    assert [a.id for a in (await repo.list_for_turns(health, [user_turn_id]))[user_turn_id]] == [
        att.id
    ]


def test_domain_for_session_rule() -> None:
    # Single domain → that domain; zero or multiple → the shared 'general' scope.
    assert domain_for_session(("health",)) == "health"
    assert domain_for_session(()) == "general"
    assert domain_for_session(("health", "finance")) == "general"
