"""Migration 0018 against real Postgres: the Proposal firewall (CLAUDE.md rule 3,
ASSISTANT.md invariants #7/#8).

A proposal is owner-only and single-domain — you cannot stage a write to a domain
the session cannot read, and a non-owner principal stages and reads none. Nodes
inherit their proposal's visibility.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
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


async def _owner_principal(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


async def _stage(maker: async_sessionmaker, pid: str, code: str, title: str) -> str:
    async with scoped_session(maker, OWNER) as session:
        prop_id = (
            await session.execute(
                text(
                    "INSERT INTO app.proposals (principal_id, kind, domain_code, title)"
                    " VALUES (:pid, 'correction', :code, :title) RETURNING id"
                ),
                {"pid": pid, "code": code, "title": title},
            )
        ).scalar()
        await session.execute(
            text(
                "INSERT INTO app.proposal_nodes (proposal_id, type, label)"
                " VALUES (:pid, 'leaf', :label)"
            ),
            {"pid": str(prop_id), "label": f"{title} leaf"},
        )
    return str(prop_id)


async def test_proposals_owner_only_and_domain_narrowed(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    tag = uuid.uuid4().hex[:8]
    for code in ("general", "health", "finance"):
        await _stage(maker, pid, code, f"{tag} {code}")
    like = {"t": f"{tag}%"}

    # A health-only owner session sees only the health proposal.
    health = read_context(pid, ("health",))
    async with scoped_session(maker, health) as session:
        rows = list(
            (
                await session.execute(
                    text("SELECT domain_code FROM app.proposals WHERE title LIKE :t"), like
                )
            ).scalars()
        )
    assert rows == ["health"]

    # The unnarrowed owner sees all three; a non-owner sees none (#8).
    async with scoped_session(maker, OWNER) as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM app.proposals WHERE title LIKE :t"), like
            )
        ).scalar() == 3
    token = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    async with scoped_session(maker, token) as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM app.proposals WHERE title LIKE :t"), like
            )
        ).scalar() == 0


async def test_narrowed_owner_cannot_stage_outside_scope(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    health = read_context(pid, ("health",))
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, health) as session:
            await session.execute(
                text(
                    "INSERT INTO app.proposals (principal_id, kind, domain_code, title)"
                    " VALUES (:pid, 'correction', 'finance', 'sneaky')"
                ),
                {"pid": pid},
            )


async def test_nodes_follow_their_proposal(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    tag = uuid.uuid4().hex[:8]
    prop_id = await _stage(maker, pid, "health", f"{tag} health")
    count_nodes = text("SELECT count(*) FROM app.proposal_nodes WHERE proposal_id = :pid")

    # A general-only session can't see the health proposal's nodes; a health one can.
    async with scoped_session(maker, read_context(pid, ("general",))) as session:
        assert (await session.execute(count_nodes, {"pid": prop_id})).scalar() == 0
    async with scoped_session(maker, read_context(pid, ("health",))) as session:
        assert (await session.execute(count_nodes, {"pid": prop_id})).scalar() == 1


async def test_notes_carry_provenance(maker: async_sessionmaker) -> None:
    # An agent-authored note is flagged and source-attributed (#7); existing notes
    # default to human-authored.
    tag = uuid.uuid4().hex[:8]
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, provenance, source_ref)"
                " VALUES (gen_random_uuid(), :cid, 'health', 'agent note', 'agent', :ref)"
            ),
            {"cid": f"{tag}-agent", "ref": "proposal:123"},
        )
        await session.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (gen_random_uuid(), :cid, 'health', 'human note')"
            ),
            {"cid": f"{tag}-human"},
        )
        result = (
            await session.execute(
                text(
                    "SELECT provenance, count(*) FROM app.notes WHERE client_id LIKE :t"
                    " GROUP BY provenance"
                ),
                {"t": f"{tag}%"},
            )
        ).all()
        rows = {r[0]: r[1] for r in result}
    assert rows == {"agent": 1, "human": 1}
