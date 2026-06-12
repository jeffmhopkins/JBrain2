"""propose_correction stages (never writes), and the agent-note executor creates a
provenance-flagged, source-attributed, idempotent note (docs/ASSISTANT.md #7)."""

from jbrain.agent.loop import ToolContext
from jbrain.agent.proposals import NodeRow, ProposalRow, ProposalSpec
from jbrain.agent.proposaltools import agent_note_executor, build_proposal_handlers
from jbrain.db.session import SessionContext

CTX = ToolContext(
    session=SessionContext(principal_kind="owner", principal_id="p1", domain_scopes=("health",)),
    scopes=("health",),
)


class FakeProposalRepo:
    def __init__(self) -> None:
        self.staged: list[tuple[str, ProposalSpec]] = []

    async def stage(self, ctx: object, *, principal_id: str, spec: ProposalSpec) -> str:
        self.staged.append((principal_id, spec))
        return "prop-1"


def handler(repo: FakeProposalRepo):
    return build_proposal_handlers(repo)["propose_correction"]  # type: ignore[arg-type]


async def test_propose_correction_stages_a_correction_proposal() -> None:
    repo = FakeProposalRepo()
    out = await handler(repo)({"correction": "PCP is Dr. Lin", "domain": "health"}, CTX)
    assert "Staged" in out and "prop-1" in out
    principal_id, spec = repo.staged[0]
    assert principal_id == "p1"
    assert spec.kind == "correction" and spec.domain == "health"
    assert spec.nodes[0].op == "add_note"
    assert spec.nodes[0].preview["body"] == "PCP is Dr. Lin"


async def test_propose_correction_refuses_an_out_of_scope_domain() -> None:
    repo = FakeProposalRepo()
    out = await handler(repo)({"correction": "x", "domain": "finance"}, CTX)
    assert "isn't scoped" in out
    assert repo.staged == []  # nothing staged outside the session's scope


async def test_propose_correction_needs_text() -> None:
    out = await handler(FakeProposalRepo())({"correction": "  "}, CTX)
    assert "needs the correction" in out


class FakeNotes:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_note(self, ctx: object, **kwargs: object) -> tuple[None, bool]:
        self.created.append(kwargs)
        return None, True


async def test_agent_note_executor_writes_a_flagged_idempotent_note() -> None:
    notes = FakeNotes()
    proposal = ProposalRow("prop-1", "correction", "approved", "health", "t", None)
    node = NodeRow(
        "node-1",
        None,
        "leaf",
        "add_note",
        "lbl",
        {"body": "the fact", "domain": "health"},
        (),
        "approved",
    )
    await agent_note_executor(notes)(CTX.session, proposal, node)  # type: ignore[arg-type]
    n = notes.created[0]
    assert n["provenance"] == "agent"
    assert n["source_ref"] == "proposal:prop-1"
    assert n["client_id"] == "proposal-node-1"  # idempotent on the node id
    assert n["body"] == "the fact"


async def test_executor_skips_an_empty_body() -> None:
    notes = FakeNotes()
    proposal = ProposalRow("p", "correction", "approved", "health", "t", None)
    node = NodeRow("n", None, "leaf", "add_note", "", {"body": "  "}, (), "approved")
    await agent_note_executor(notes)(CTX.session, proposal, node)  # type: ignore[arg-type]
    assert notes.created == []
