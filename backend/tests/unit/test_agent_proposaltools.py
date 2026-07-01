"""propose_correction stages (never writes), and the agent-note executor creates a
provenance-flagged, source-attributed, idempotent note (docs/ASSISTANT.md #7)."""

from jbrain.agent.contracts import ProposalRef
from jbrain.agent.loop import ToolContext, ToolOutput
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
    assert "Staged" in out
    # The id rides structurally (a "Review proposal" chip), never in the prose.
    assert isinstance(out, ToolOutput)
    assert out.proposal == ProposalRef(proposal_id="prop-1", kind="correction")
    assert "prop-1" not in out
    principal_id, spec = repo.staged[0]
    assert principal_id == "p1"
    assert spec.kind == "correction" and spec.domain == "health"
    assert spec.nodes[0].op == "add_note"
    assert spec.nodes[0].preview["body"] == "PCP is Dr. Lin"
    # The proposal carries no chat session (this CTX has none) — a session-less
    # proposal surfaces in every session's inbox.
    assert spec.session_id is None


async def test_propose_correction_ties_the_proposal_to_the_chat_session() -> None:
    repo = FakeProposalRepo()
    ctx = ToolContext(session=CTX.session, scopes=("health",), agent_session_id="sess-9")
    await handler(repo)({"correction": "PCP is Dr. Lin", "domain": "health"}, ctx)
    _, spec = repo.staged[0]
    # The chat's session id rides onto the staged proposal, so the review inbox can
    # scope it to that session.
    assert spec.session_id == "sess-9"


async def test_propose_correction_refuses_an_out_of_scope_domain() -> None:
    repo = FakeProposalRepo()
    out = await handler(repo)({"correction": "x", "domain": "finance"}, CTX)
    assert "isn't scoped" in out
    assert repo.staged == []  # nothing staged outside the session's scope


async def test_propose_correction_needs_text() -> None:
    out = await handler(FakeProposalRepo())({"correction": "  "}, CTX)
    assert "needs the correction" in out


class FakeNote:
    def __init__(self, note_id: str) -> None:
        self.id = note_id


class FakeNotes:
    def __init__(self, created: bool = True) -> None:
        self.created: list[dict] = []
        self._created = created

    async def create_note(self, ctx: object, **kwargs: object) -> tuple[FakeNote, bool]:
        self.created.append(kwargs)
        return FakeNote(f"note-for-{kwargs['client_id']}"), self._created


class FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, ctx: object, kind: str, payload: dict) -> str:
        self.enqueued.append((kind, payload))
        return "job-1"


async def test_agent_note_executor_writes_a_flagged_idempotent_note_and_enqueues_ingest() -> None:
    notes, jobs = FakeNotes(), FakeJobs()
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
    await agent_note_executor(notes, jobs)(CTX.session, proposal, node)  # type: ignore[arg-type]
    n = notes.created[0]
    assert n["provenance"] == "agent"
    assert n["source_ref"] == "proposal:prop-1"
    assert n["client_id"] == "proposal-node-1"  # idempotent on the node id
    assert n["body"] == "the fact"
    # The note re-enters ingestion just like a captured one (else it'd stay 'pending').
    assert jobs.enqueued == [("ingest_note", {"note_id": "note-for-proposal-node-1"})]


async def test_executor_skips_enqueue_on_an_idempotent_re_enact() -> None:
    notes, jobs = FakeNotes(created=False), FakeJobs()
    proposal = ProposalRow("p", "correction", "approved", "health", "t", None)
    node = NodeRow("n", None, "leaf", "add_note", "lbl", {"body": "again"}, (), "approved")
    await agent_note_executor(notes, jobs)(CTX.session, proposal, node)  # type: ignore[arg-type]
    assert jobs.enqueued == []  # the note already exists — no duplicate ingest


async def test_executor_skips_an_empty_body() -> None:
    notes, jobs = FakeNotes(), FakeJobs()
    proposal = ProposalRow("p", "correction", "approved", "health", "t", None)
    node = NodeRow("n", None, "leaf", "add_note", "", {"body": "  "}, (), "approved")
    await agent_note_executor(notes, jobs)(CTX.session, proposal, node)  # type: ignore[arg-type]
    assert notes.created == [] and jobs.enqueued == []


# --- make_intake_link: stages an editable intake-link Proposal, never mints --------


def _intake_handler(repo: "FakeProposalRepo"):
    from jbrain.agent.proposaltools import build_intake_link_handlers

    return build_intake_link_handlers(repo)["make_intake_link"]  # type: ignore[arg-type]


async def test_make_intake_link_stages_an_editable_intake_link_proposal() -> None:
    repo = FakeProposalRepo()
    out = await _intake_handler(repo)(
        {
            "subject_id": "subj-1",
            "domain": "health",
            "fields_brief": "their current medications",
            "persona_brief": "warm and patient",
            "opening_blurb": "hi there",
            "max_runs": 3,
            "bind_on_first": True,
        },
        CTX,
    )
    assert isinstance(out, ToolOutput)
    assert out.proposal == ProposalRef(proposal_id="prop-1", kind="intake-link")
    principal_id, spec = repo.staged[0]
    assert principal_id == "p1"
    assert spec.kind == "intake-link" and spec.domain == "health" and spec.subject_id == "subj-1"
    leaf = spec.nodes[0]
    assert leaf.op == "mint_intake_link"  # never an add_note — it must not enact as a note
    assert leaf.preview["fields_brief"] == "their current medications"
    # The assistant-drafted interviewer persona flows into the editable preview (v2).
    assert leaf.preview["persona_brief"] == "warm and patient"
    assert leaf.preview["max_runs"] == 3 and leaf.preview["bind_on_first"] is True
    # max_opens defaults to 4x max_runs; ttl defaults to 24h.
    assert leaf.preview["max_opens"] == 12 and leaf.preview["ttl_hours"] == 24.0


async def test_make_intake_link_refuses_out_of_scope_domain_and_missing_fields() -> None:
    repo = FakeProposalRepo()
    assert "isn't scoped" in await _intake_handler(repo)(
        {
            "subject_id": "s",
            "domain": "finance",
            "fields_brief": "x",
            "max_runs": 1,
            "bind_on_first": False,
        },
        CTX,
    )
    assert "subject_id" in await _intake_handler(repo)(
        {"domain": "health", "fields_brief": "x", "max_runs": 1, "bind_on_first": False}, CTX
    )
    assert "max_runs" in await _intake_handler(repo)(
        {
            "subject_id": "s",
            "domain": "health",
            "fields_brief": "x",
            "max_runs": 0,
            "bind_on_first": False,
        },
        CTX,
    )
    assert repo.staged == []  # nothing staged on any refusal
