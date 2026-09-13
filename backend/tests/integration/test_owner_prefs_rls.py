"""Migration 0195 against real Postgres: `owner_prefs` is owner-only (CLAUDE.md rule 3).

The mandatory per-new-table RLS isolation test for the owner's standing instructions
(docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md D15). The owner round-trips a rule list; a
non-owner (capability-token) principal sees ZERO rows and cannot write — the owner
policy's WITH CHECK blocks it. The tools are exercised through the same RLS-scoped
session, which is the only place their staging behaviour meets the real Proposal engine:
`prefs_write` must leave the document byte-identical and put an `owner-prefs` row in
`app.proposals` instead, and the document must change only when the trusted executor
runs the leaf the owner approved.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.prefstools import (
    PREFS_KIND,
    build_owner_prefs_handlers,
    owner_prefs_executor,
)
from jbrain.agent.proposals import ProposalRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.owner_prefs import OwnerPrefsRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A non-owner principal: a capability token with no owner identity — app.is_owner() is false.
NON_OWNER = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


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
    return SessionContext(principal_id=str(pid), principal_kind="owner", domain_scopes=("general",))


async def test_owner_round_trips_a_rule_list(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    repo = OwnerPrefsRepo()

    async with scoped_session(maker, owner) as session:
        assert await repo.read_rules(session, owner.principal_id) == []  # empty before any write
        await repo.write_rules(session, owner.principal_id, ["keep recipes whole"])

    async with scoped_session(maker, owner) as session:
        assert await repo.read_rules(session, owner.principal_id) == ["keep recipes whole"]
        await repo.write_rules(session, owner.principal_id, ["keep recipes whole", "no splitting"])

    async with scoped_session(maker, owner) as session:
        assert await repo.read_rules(session, owner.principal_id) == [
            "keep recipes whole",
            "no splitting",
        ]


async def test_non_owner_sees_nothing_and_cannot_write(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    repo = OwnerPrefsRepo()

    async with scoped_session(maker, owner) as session:
        await repo.write_rules(session, owner.principal_id, ["owner-only rule"])

    # A non-owner principal sees zero rows — RLS hides the standing instructions entirely.
    async with scoped_session(maker, NON_OWNER) as session:
        count = (await session.execute(text("SELECT count(*) FROM app.owner_prefs"))).scalar()
    assert count == 0

    # …and cannot write: the owner WITH CHECK rejects a non-owner insert.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, NON_OWNER) as session:
            await repo.write_rules(session, "sneaky", ["should be blocked"])

    # The owner's document is intact and unchanged.
    async with scoped_session(maker, owner) as session:
        assert await repo.read_rules(session, owner.principal_id) == ["owner-only rule"]


async def _content(maker: async_sessionmaker, owner: SessionContext) -> str:
    async with scoped_session(maker, owner) as session:
        return (
            await session.execute(
                text("SELECT content FROM app.owner_prefs WHERE principal_id = :p"),
                {"p": owner.principal_id},
            )
        ).scalar() or ""


async def test_prefs_write_stages_a_real_proposal_and_writes_only_on_enact(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner(maker)
    async with scoped_session(maker, owner) as session:
        await OwnerPrefsRepo().write_rules(session, owner.principal_id, ["keep recipes whole"])

    proposals = ProposalRepo(maker)
    handlers = build_owner_prefs_handlers(maker, proposals)
    ctx = ToolContext(session=owner, scopes=("general",))

    listing = await handlers["prefs_read"]({}, ctx)
    assert "1. keep recipes whole" in listing

    out = await handlers["prefs_write"](
        {"op": "add", "text": "stop splitting ingredients", "rule_number": 0}, ctx
    )
    assert isinstance(out, ToolOutput)
    # Nothing changed — the owner's approval is the only write path (D17).
    assert await _content(maker, owner) == "keep recipes whole"
    assert out.proposal is not None
    proposal_id = out.proposal.proposal_id

    # `owner-prefs` is a real kind the 0195 CHECK admits — a staged row the inbox lists.
    open_now = await proposals.list_open(owner)
    assert [p.kind for p in open_now if p.id == proposal_id] == [PREFS_KIND]

    _, nodes = await proposals.load(owner, proposal_id)
    await proposals.decide(owner, nodes[0].id, approve=True)
    plan = await proposals.enact(owner, proposal_id, owner_prefs_executor(maker))
    assert plan.enactable == (nodes[0].id,)
    assert await _content(maker, owner) == "keep recipes whole\nstop splitting ingredients"


async def test_a_rejected_edit_never_reaches_the_document(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    async with scoped_session(maker, owner) as session:
        await OwnerPrefsRepo().write_rules(session, owner.principal_id, ["keep recipes whole"])

    proposals = ProposalRepo(maker)
    handlers = build_owner_prefs_handlers(maker, proposals)
    ctx = ToolContext(session=owner, scopes=("general",))
    out = await handlers["prefs_write"](
        {"op": "remove", "text": "keep recipes whole", "rule_number": 1}, ctx
    )
    assert isinstance(out, ToolOutput) and out.proposal is not None

    _, nodes = await proposals.load(owner, out.proposal.proposal_id)
    await proposals.decide(owner, nodes[0].id, approve=False, reason="no, I want that one")
    plan = await proposals.enact(owner, out.proposal.proposal_id, owner_prefs_executor(maker))
    assert plan.enactable == () and plan.held == ()
    assert await _content(maker, owner) == "keep recipes whole"
