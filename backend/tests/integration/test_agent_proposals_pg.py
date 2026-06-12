"""The Proposal repo against real Postgres: stage a tree, approve part of it, and
enact — proving the dependency-safe rule end to end (an approved leaf with a
rejected prerequisite is held, never enacted). RLS isolation is in
test_agent_proposals_rls.py."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.proposals import (
    NodeRow,
    NodeSpec,
    ProposalRepo,
    ProposalRow,
    ProposalSpec,
)
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


async def test_stage_decide_enact_round_trip(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)

    # A two-leaf tree where b depends on a.
    a, b, root = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    spec = ProposalSpec(
        kind="knowledge",
        domain="health",
        title="add two facts",
        nodes=[
            NodeSpec(root, "group", label="root"),
            NodeSpec(a, "leaf", op="add_note", label="fact a", parent_id=root),
            NodeSpec(b, "leaf", op="add_note", label="fact b", parent_id=root, deps=(a,)),
        ],
    )
    prop_id = await repo.stage(OWNER, principal_id=pid, spec=spec)

    # Approve the whole tree, then enact — both leaves run, in any order.
    await repo.decide(OWNER, root, approve=True)
    enacted: list[str] = []

    async def executor(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        enacted.append(node.label)

    plan = await repo.enact(OWNER, prop_id, executor)
    assert set(plan.enactable) == {a, b} and plan.held == ()
    assert set(enacted) == {"fact a", "fact b"}
    _, nodes = await repo.load(OWNER, prop_id)
    assert {n.id: n.status for n in nodes if n.type == "leaf"} == {a: "enacted", b: "enacted"}


async def test_a_rejected_prerequisite_holds_its_dependent(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    spec = ProposalSpec(
        kind="knowledge",
        domain="general",
        title="dependent",
        nodes=[
            NodeSpec(a, "leaf", label="prereq"),
            NodeSpec(b, "leaf", label="dependent", deps=(a,)),
        ],
    )
    prop_id = await repo.stage(OWNER, principal_id=pid, spec=spec)

    # Approve b but reject its prerequisite a.
    await repo.decide(OWNER, b, approve=True)
    await repo.decide(OWNER, a, approve=False)

    ran: list[str] = []

    async def executor(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        ran.append(node.label)

    plan = await repo.enact(OWNER, prop_id, executor)
    # b is held (its prereq was rejected), nothing ran — fail-closed.
    assert plan.enactable == () and plan.held == (b,)
    assert ran == []
    _, nodes = await repo.load(OWNER, prop_id)
    assert {n.id: n.status for n in nodes}[b] == "held"
