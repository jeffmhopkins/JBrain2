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
from jbrain.agent.session import AgentSessionRepo
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


async def test_decline_reason_persists_on_the_declined_node(maker: async_sessionmaker) -> None:
    """A decline reason is recorded on the explicitly-declined node (not its subtree),
    survives reload, and an approve carries none — INLINE_APPROVALS_PLAN §3.3."""
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    spec = ProposalSpec(
        kind="appointment",
        domain="health",
        title="two changes",
        nodes=[
            NodeSpec(a, "leaf", op="add_note", label="keep"),
            NodeSpec(b, "leaf", op="manage_appointment", label="reschedule"),
        ],
    )
    prop_id = await repo.stage(OWNER, principal_id=pid, spec=spec)

    await repo.decide(OWNER, a, approve=True)
    await repo.decide(OWNER, b, approve=False, reason="wrong date")

    _, nodes = await repo.load(OWNER, prop_id)
    by_id = {n.id: n for n in nodes}
    assert by_id[b].status == "rejected" and by_id[b].decision_note == "wrong date"
    assert by_id[a].decision_note is None  # an approved node carries no reason


async def test_declining_then_approving_clears_the_reason(maker: async_sessionmaker) -> None:
    """Decision #3 / decide() docstring: a re-approved node carries no reason — decline
    with a reason, then approve, and decision_note is back to NULL."""
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    a = str(uuid.uuid4())
    spec = ProposalSpec(
        kind="correction",
        domain="general",
        title="one",
        nodes=[NodeSpec(a, "leaf", op="add_note", label="fact")],
    )
    prop_id = await repo.stage(OWNER, principal_id=pid, spec=spec)

    await repo.decide(OWNER, a, approve=False, reason="not accurate")
    _, nodes = await repo.load(OWNER, prop_id)
    assert nodes[0].decision_note == "not accurate"

    await repo.decide(OWNER, a, approve=True)
    _, nodes = await repo.load(OWNER, prop_id)
    assert nodes[0].status == "approved" and nodes[0].decision_note is None


async def test_edited_leaf_enacts_as_a_human_authored_note(maker: async_sessionmaker) -> None:
    """End-to-end Decision #2: patch a note leaf, then enact through the REAL
    agent_note_executor, and the note lands provenance='human' with an #edited
    source_ref — the owner's correction, not the agent's."""
    from jbrain.agent.proposaltools import agent_note_executor
    from jbrain.notes.repo import SqlNotesRepo

    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    a = str(uuid.uuid4())
    spec = ProposalSpec(
        kind="correction",
        domain="health",
        title="dose",
        nodes=[NodeSpec(a, "leaf", op="add_note", label="HCTZ", preview={"body": "12.5 mg"})],
    )
    prop_id = await repo.stage(OWNER, principal_id=pid, spec=spec)
    assert await repo.patch_node_body(OWNER, a, "25 mg daily") is True
    await repo.decide(OWNER, a, approve=True)

    class _Jobs:
        def __init__(self) -> None:
            self.enqueued: list[tuple[str, dict]] = []

        async def enqueue(self, ctx: object, kind: str, payload: dict) -> str:
            self.enqueued.append((kind, payload))
            return "job-1"

    executor = agent_note_executor(SqlNotesRepo(maker), _Jobs())  # type: ignore[arg-type]
    plan = await repo.enact(OWNER, prop_id, executor)
    assert plan.enactable == (a,)

    async with scoped_session(maker, OWNER) as session:
        row = (
            await session.execute(
                text("SELECT body, provenance, source_ref FROM app.notes WHERE client_id = :cid"),
                {"cid": f"proposal-{a}"},
            )
        ).one()
    assert row.body == "25 mg daily"
    assert row.provenance == "human"
    assert row.source_ref == f"proposal:{prop_id}#edited"


async def test_correct_in_place_edits_body_and_flags_edited(maker: async_sessionmaker) -> None:
    """patch_node_body rewrites a staged note/appointment leaf's body and flags it
    `edited`; it no-ops on a non-editable op, an unknown id, or a decided proposal —
    INLINE_APPROVALS_PLAN §3.2."""
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    note_id, mint_id = str(uuid.uuid4()), str(uuid.uuid4())
    spec = ProposalSpec(
        kind="correction",
        domain="health",
        title="dose",
        nodes=[
            NodeSpec(note_id, "leaf", op="add_note", label="HCTZ", preview={"body": "12.5 mg"}),
            NodeSpec(mint_id, "leaf", op="mint_intake_link", label="link", preview={"body": "x"}),
        ],
    )
    prop_id = await repo.stage(OWNER, principal_id=pid, spec=spec)

    assert await repo.patch_node_body(OWNER, note_id, "25 mg") is True
    # A non-editable op (mint_intake_link) and an unknown id both no-op.
    assert await repo.patch_node_body(OWNER, mint_id, "hijack") is False
    assert await repo.patch_node_body(OWNER, str(uuid.uuid4()), "ghost") is False
    # An empty body is rejected before touching the DB.
    assert await repo.patch_node_body(OWNER, note_id, "   ") is False

    _, nodes = await repo.load(OWNER, prop_id)
    edited = {n.id: n for n in nodes}[note_id]
    assert edited.preview["body"] == "25 mg" and edited.preview["edited"] is True

    # Approving the node leaves the proposal 'staged' — still editable right up to enact.
    await repo.decide(OWNER, note_id, approve=True)
    assert await repo.patch_node_body(OWNER, note_id, "37.5 mg") is True

    # Once enacted, the proposal is no longer staged — further edits are refused.
    async def _noop(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        return None

    await repo.enact(OWNER, prop_id, _noop)
    assert await repo.patch_node_body(OWNER, note_id, "50 mg") is False


async def test_list_open_scopes_to_session_plus_session_less(maker: async_sessionmaker) -> None:
    """The session-scoped inbox is a chat's own staged proposals plus the
    session-less (background) ones — never another chat's."""
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    # Real chat sessions: proposals.session_id is FK-bound to agent_sessions, and
    # agent_sessions.principal_id is FK-bound to principals — so create under a
    # context carrying the real owner pid (OWNER's is a random uuid).
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    sessions = AgentSessionRepo(maker)
    chat_a = await sessions.create(owner, domain_scopes=["general"], title="A")
    chat_b = await sessions.create(owner, domain_scopes=["general"], title="B")

    def one(title: str, session_id: str | None) -> ProposalSpec:
        return ProposalSpec(
            kind="correction",
            domain="general",
            title=title,
            nodes=[NodeSpec(str(uuid.uuid4()), "leaf", op="add_note", label=title)],
            session_id=session_id,
        )

    sid_a, sid_b = chat_a.id, chat_b.id
    await repo.stage(OWNER, principal_id=pid, spec=one("from chat A", sid_a))
    await repo.stage(OWNER, principal_id=pid, spec=one("from chat B", sid_b))
    await repo.stage(OWNER, principal_id=pid, spec=one("from nightly", None))

    # Unscoped: the see-everything list carries all three (other tests in this
    # file share the DB, so assert membership, not an exact set).
    everything = {s.title for s in await repo.list_open(OWNER)}
    assert {"from chat A", "from chat B", "from nightly"} <= everything

    # Chat A: its own proposal + the session-less one, never chat B's.
    chat_a = {s.title for s in await repo.list_open(OWNER, sid_a)}
    assert "from chat A" in chat_a and "from nightly" in chat_a
    assert "from chat B" not in chat_a
