"""An `appointment` Proposal end to end against real Postgres: migration 0027
admits the kind, and an approved manage_appointment leaf enacts through the real
executor — re-entering as an agent-authored, dated note that ingestion will turn
into an appointment (notes are the sole source of truth, #7). RLS isolation for
proposals is proven in test_agent_proposals_rls.py."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.connectortools import build_leaf_executor
from jbrain.agent.proposals import NodeSpec, ProposalRepo, ProposalSpec
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.notes.repo import SqlNotesRepo
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
    return SessionContext(principal_kind="owner", principal_id=str(pid))


class _Jobs:
    """Records ingestion enqueues — the note write is real; the queue is faked."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, ctx: object, kind: str, payload: dict) -> str:
        self.enqueued.append((kind, payload))
        return "job-1"


async def test_appointment_proposal_enacts_as_a_dated_agent_note(
    maker: async_sessionmaker,
) -> None:
    ctx = await _owner(maker)
    repo = ProposalRepo(maker)
    body = "Dentist with Dr. Nguyen is scheduled for 2026-07-01 at 2pm."

    # The shape manage_appointment stages: an `appointment` kind (admitted by the
    # 0027 CHECK) whose leaf carries the composed note body in its preview.
    node = NodeSpec(
        id="11111111-1111-1111-1111-111111111111",
        type="leaf",
        op="manage_appointment",
        label="add dentist",
        preview={"body": body, "domain": "general", "action": "create"},
    )
    spec = ProposalSpec(kind="appointment", domain="general", title="add dentist", nodes=[node])
    prop_id = await repo.stage(ctx, principal_id=str(ctx.principal_id), spec=spec)

    await repo.decide(ctx, node.id, approve=True)

    jobs = _Jobs()
    executor = build_leaf_executor(SqlNotesRepo(maker), object(), jobs)  # type: ignore[arg-type]
    plan = await repo.enact(ctx, prop_id, executor)
    assert plan.enactable == (node.id,)

    # The approved change re-entered as an agent-authored, source-attributed note,
    # and ingestion was enqueued so it actually indexes and gets analyzed.
    async with scoped_session(maker, OWNER) as session:
        row = (
            await session.execute(
                text("SELECT body, provenance, source_ref FROM app.notes WHERE source_ref = :ref"),
                {"ref": f"proposal:{prop_id}"},
            )
        ).one()
    assert row.body == body and row.provenance == "agent"
    assert len(jobs.enqueued) == 1
    kind, payload = jobs.enqueued[0]
    assert kind == "ingest_note" and "note_id" in payload
