"""Migration 0015 against real Postgres: the owner-narrowable domain firewall and
agent_sessions RLS (CLAUDE.md rule 3, ASSISTANT.md invariant #4).

Proves the load-bearing security property: a narrowed owner session is restricted
to its selected domains by Postgres, not by the tools — while an ordinary owner
session still sees everything (the backward-compatibility regression).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo, read_context
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


async def _seed_notes(maker: async_sessionmaker, run: str) -> None:
    async with scoped_session(maker, OWNER) as session:
        for code in ("general", "health", "finance"):
            await session.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (gen_random_uuid(), :cid, :code, :body)"
                ),
                {"cid": f"{run}-{code}", "code": code, "body": f"{run} {code}"},
            )


async def test_owner_scoped_narrows_domain_reads(maker: async_sessionmaker) -> None:
    run = uuid.uuid4().hex[:8]
    await _seed_notes(maker, run)
    like = {"p": f"{run}-%"}

    # A narrowed (health-only) owner sees ONLY health — the firewall, not a filter.
    health = read_context(str(uuid.uuid4()), ("health",))
    async with scoped_session(maker, health) as session:
        rows = list(
            (
                await session.execute(
                    text("SELECT domain_code FROM app.notes WHERE client_id LIKE :p"), like
                )
            ).scalars()
        )
    assert rows == ["health"]

    # Regression: an ordinary (unnarrowed) owner still sees all three domains.
    async with scoped_session(maker, OWNER) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM app.notes WHERE client_id LIKE :p"), like
            )
        ).scalar()
    assert count == 3


async def test_narrowed_owner_cannot_write_outside_scope(maker: async_sessionmaker) -> None:
    health = read_context(str(uuid.uuid4()), ("health",))
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, health) as session:
            await session.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (gen_random_uuid(), :cid, 'finance', 'sneaky')"
                ),
                {"cid": f"sneak-{uuid.uuid4().hex[:8]}"},
            )


async def test_agent_sessions_are_owner_only(maker: async_sessionmaker) -> None:
    auth = SqlAuthRepo(maker)
    await service.rotate_owner_key(auth)
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    owner = SessionContext(principal_id=str(pid), principal_kind="owner")

    repo = AgentSessionRepo(maker)
    info = await repo.create(owner, domain_scopes=["health"], title="health cleanup")
    assert info.domain_scopes == ("health",)
    assert len(await repo.list(owner)) == 1

    # A non-owner principal sees no sessions at all.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    assert await repo.list(token) == []

    # A *narrowed* owner still sees its sessions — owner_scoped restricts domain
    # data, never owner-only tables (it keeps owner identity).
    narrowed = read_context(str(pid), ("general",))
    assert len(await repo.list(narrowed)) == 1


async def _owner_ctx(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def test_rename_updates_the_title(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    info = await repo.create(owner, domain_scopes=["general"], title="old")
    await repo.rename(owner, info.id, "new name")
    assert (await repo.get(owner, info.id)).title == "new name"  # type: ignore[union-attr]


async def test_delete_cascades_runs_and_transcript_and_is_owner_only(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner_ctx(maker)
    repo = AgentSessionRepo(maker)
    info = await repo.create(owner, domain_scopes=["general"], title="scratch")
    run_id = await AgentRunLog(maker).start(owner, session_id=info.id, prompt_version="v")
    await AgentTranscript(maker).record_exchange(
        owner, session_id=info.id, run_id=run_id, user_text="q", assistant_text="a", tools=[]
    )

    # A non-owner cannot delete it (RLS blocks the row); it survives.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    await repo.delete(token, info.id)
    assert await repo.get(owner, info.id) is not None

    # The owner deletes it; the run and the transcript cascade away.
    await repo.delete(owner, info.id)
    assert await repo.get(owner, info.id) is None
    async with scoped_session(maker, owner) as session:
        runs = (
            await session.execute(
                text("SELECT count(*) FROM app.agent_runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar()
        turns = (
            await session.execute(
                text("SELECT count(*) FROM app.agent_turns WHERE session_id = :id"), {"id": info.id}
            )
        ).scalar()
    assert runs == 0 and turns == 0
