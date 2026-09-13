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

from jbrain.agent.prefstools import PREFS_KIND
from jbrain.agent.proposals import (
    INSTRUCTION_PROPOSAL_KINDS,
    LeafRefused,
    NodeRow,
    NodeSpec,
    ProposalRepo,
    ProposalRow,
    ProposalSpec,
    enact_outcome_summary,
)
from jbrain.agent.session import AgentSessionRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.note_conversation import NoteConversationRepo
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


async def test_list_waiting_approvals_is_the_notes_tabs_half(maker: async_sessionmaker) -> None:
    """D4/D17: anything staged inside a NOTE CONVERSATION waits on the review inbox's
    notes tab. An ordinary chat's proposal does not (it keeps that chat's inline
    approvals) and neither does a background one — it has no thread to redirect to.

    The KIND arm (`INSTRUCTION_PROPOSAL_KINDS`) is exercised too, at the bottom. It was
    declined here on a premise that had already expired — "`owner_prefs` is not yet in
    `proposals_kind_check`" — when 0195 is on this branch and admits `owner-prefs`. The
    set meanwhile held the UNDERSCORE spelling and so matched nothing that could ever be
    staged, which is what an untested arm buys: the union exists precisely so a standing
    -instruction change is findable when it is staged OUTSIDE a note thread, and that is
    the one case the conversation arm cannot carry."""
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    owner = SessionContext(principal_id=pid, principal_kind="owner")
    sessions = AgentSessionRepo(maker)
    chat = await sessions.create(owner, domain_scopes=["general"], title="chat")
    thread = await sessions.create(owner, domain_scopes=["general"], title="note thread")

    # A note conversation over that second session, so the "staged inside a note
    # conversation" arm has something to match.
    note_id = str(uuid.uuid4())
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (CAST(:id AS uuid), :cid, 'general', 'seed')"
            ),
            {"id": note_id, "cid": f"prop-inbox-{note_id[:12]}"},
        )
    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().start(s, session_id=thread.id, note_id=note_id, body_sha="sha")

    def one(kind: str, title: str, session_id: str | None) -> ProposalSpec:
        return ProposalSpec(
            kind=kind,
            domain="general",
            title=title,
            nodes=[NodeSpec(str(uuid.uuid4()), "leaf", op="add_note", label=title)],
            session_id=session_id,
        )

    await repo.stage(OWNER, principal_id=pid, spec=one("correction", "in a thread", thread.id))
    await repo.stage(OWNER, principal_id=pid, spec=one("correction", "an ordinary chat", chat.id))
    await repo.stage(OWNER, principal_id=pid, spec=one("correction", "from nightly", None))

    waiting = {w.title: w for w in await repo.list_waiting_approvals(OWNER)}
    assert "in a thread" in waiting
    assert "an ordinary chat" not in waiting
    assert "from nightly" not in waiting
    # What a redirect needs: the session to open and the persona hosting it.
    assert waiting["in a thread"].session_id == thread.id
    assert waiting["in a thread"].agent == "curator"

    # And an APPROVED proposal drops out: it is waiting on the enact, not on a decision.
    _, nodes = await repo.load(OWNER, next(iter(waiting.values())).id)
    await repo.decide(OWNER, nodes[0].id, approve=True)
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text("UPDATE app.proposals SET status = 'approved' WHERE title = 'in a thread'")
        )
    assert "in a thread" not in {w.title for w in await repo.list_waiting_approvals(OWNER)}

    # The kind arm, standing on its own: an `owner-prefs` proposal staged from an
    # ORDINARY chat — no note conversation to match on — still waits on the notes tab.
    # This is the scenario the union was written for, and the only one that fails if the
    # spelling drifts from `prefstools.PREFS_KIND` again.
    assert PREFS_KIND in INSTRUCTION_PROPOSAL_KINDS
    await repo.stage(OWNER, principal_id=pid, spec=one(PREFS_KIND, "a standing rule", chat.id))
    by_title = {w.title: w for w in await repo.list_waiting_approvals(OWNER)}
    assert "a standing rule" in by_title
    assert by_title["a standing rule"].session_id == chat.id


async def test_a_leaf_the_executor_refuses_is_held_and_the_proposal_is_not_enacted(
    maker: async_sessionmaker,
) -> None:
    """The owner is never told a change landed that did not.

    `enact` marked every `plan.enactable` leaf `enacted` regardless of what the executor
    did, and `owner_prefs_executor` returned silently on both its refusals (a stale
    `prev`, an over-cap edit). Staging a `replace`, moving the rules underneath it and
    then approving produced: content unchanged (correct — the stale `prev` was refused),
    proposal `enacted`, node `enacted`, `held` empty. The only trace was a structlog line
    on a box the owner reads through a debug token (CLAUDE.md #10).

    The refusal is `LeafRefused` now, caught per leaf so it cannot roll back a sibling —
    the objection that kept it swallowed, and one that never applied to `owner-prefs`
    anyway, which stages exactly one leaf per proposal."""
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    leaf = str(uuid.uuid4())
    prop_id = await repo.stage(
        OWNER,
        principal_id=pid,
        spec=ProposalSpec(
            kind=PREFS_KIND,
            domain="general",
            title="Change standing instruction: stop splitting ingredients",
            nodes=[NodeSpec(leaf, "leaf", op="refuse_me", label="stop splitting ingredients")],
        ),
    )
    await repo.decide(OWNER, leaf, approve=True)

    async def refusing(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        raise LeafRefused("rule 2 has changed since this edit was staged")

    plan = await repo.enact(OWNER, prop_id, refusing)

    assert plan.enactable == ()
    assert plan.held == (leaf,)
    proposal, nodes = await repo.load(OWNER, prop_id)
    assert [n.status for n in nodes if n.type == "leaf"] == ["held"]
    assert proposal.status != "enacted"
    # And the server-authored summary the owner reads says so, rather than counting a
    # refusal as an approval.
    summary = enact_outcome_summary(proposal, nodes, plan)
    assert "Enacted nothing" in summary
    assert "1 held, not run" in summary


async def test_a_refusal_does_not_roll_back_the_sibling_leaves_that_ran(
    maker: async_sessionmaker,
) -> None:
    """The catch is PER LEAF. This is the property the swallowing was defending, kept
    without the lie: the leaf that ran is `enacted`, the one that refused is `held`, and
    the proposal is enacted because something really did land."""
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    good, bad = str(uuid.uuid4()), str(uuid.uuid4())
    prop_id = await repo.stage(
        OWNER,
        principal_id=pid,
        spec=ProposalSpec(
            kind="knowledge",
            domain="general",
            title="two edits",
            nodes=[
                NodeSpec(good, "leaf", op="add_note", label="lands"),
                NodeSpec(bad, "leaf", op="add_note", label="refuses"),
            ],
        ),
    )
    await repo.decide(OWNER, good, approve=True)
    await repo.decide(OWNER, bad, approve=True)

    ran: list[str] = []

    async def executor(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        if node.id == bad:
            raise LeafRefused("no longer applies")
        ran.append(node.label)

    plan = await repo.enact(OWNER, prop_id, executor)

    assert ran == ["lands"]
    assert plan.enactable == (good,) and plan.held == (bad,)
    proposal, nodes = await repo.load(OWNER, prop_id)
    assert {n.id: n.status for n in nodes if n.type == "leaf"} == {good: "enacted", bad: "held"}
    assert proposal.status == "enacted"


async def test_an_executor_bug_still_propagates(maker: async_sessionmaker) -> None:
    """`LeafRefused` is a DECISION. Any other exception is a bug, and swallowing it here
    would turn a broken executor into a silently held leaf nobody investigates."""
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    leaf = str(uuid.uuid4())
    prop_id = await repo.stage(
        OWNER,
        principal_id=pid,
        spec=ProposalSpec(
            kind="knowledge",
            domain="general",
            title="boom",
            nodes=[NodeSpec(leaf, "leaf", op="add_note", label="boom")],
        ),
    )
    await repo.decide(OWNER, leaf, approve=True)

    async def broken(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        raise RuntimeError("the executor is broken")

    with pytest.raises(RuntimeError):
        await repo.enact(OWNER, prop_id, broken)
