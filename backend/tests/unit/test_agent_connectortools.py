"""Connector tools STAGE an egress Proposal (never call out), and the egress leaf
executor fires the connector only on enact (docs/ASSISTANT.md #9)."""

from types import SimpleNamespace
from typing import Any

from jbrain.agent.connectortools import (
    build_connector_handlers,
    build_leaf_executor,
    egress_executor,
)
from jbrain.agent.loop import ToolContext
from jbrain.agent.proposals import NodeRow, ProposalRow, ProposalSpec
from jbrain.connectors.base import ConnectorRegistry
from jbrain.connectors.medical import medical_connectors
from jbrain.db.session import SessionContext

REGISTRY = ConnectorRegistry(medical_connectors("https://rx.example", "https://mp.example"))
HEALTH = ToolContext(
    session=SessionContext(principal_kind="owner", principal_id="p1", domain_scopes=("health",)),
    scopes=("health",),
)


class FakeProposals:
    def __init__(self) -> None:
        self.staged: list[tuple[str, ProposalSpec]] = []

    async def stage(self, ctx: object, *, principal_id: str, spec: ProposalSpec) -> str:
        self.staged.append((principal_id, spec))
        return "prop-1"


class FakeConnectorService:
    def __init__(self) -> None:
        self.fetched: list[tuple[str, dict, str]] = []

    async def fetch(
        self, ctx: object, *, connector_name: str, params: dict, principal_id: str
    ) -> str:
        self.fetched.append((connector_name, params, principal_id))
        return "result"


def handler(proposals: FakeProposals, name: str = "lookup_medication"):
    return build_connector_handlers(REGISTRY, proposals)[name]  # type: ignore[arg-type]


async def test_connector_tool_stages_an_egress_proposal_not_a_call() -> None:
    proposals = FakeProposals()
    out = await handler(proposals)({"name": "metformin"}, HEALTH)
    assert "staged" in out.lower() and "prop-1" in out
    _, spec = proposals.staged[0]
    assert spec.kind == "egress" and spec.domain == "health"
    node = spec.nodes[0]
    assert node.op == "egress_call"
    assert node.preview["connector"] == "lookup_medication"
    assert node.preview["params"] == {"name": "metformin"}
    assert node.preview["url"] == "https://rx.example/REST/drugs.json"


async def test_connector_tool_refuses_an_out_of_scope_domain() -> None:
    proposals = FakeProposals()
    general = ToolContext(
        session=SessionContext(principal_kind="owner", principal_id="p1"), scopes=("general",)
    )
    out = await handler(proposals)({"name": "metformin"}, general)
    assert "isn't scoped to health" in out
    assert proposals.staged == []  # nothing staged outside scope


async def test_connector_tool_reports_a_guard_rejection() -> None:
    proposals = FakeProposals()
    out = await handler(proposals)({}, HEALTH)  # missing the required name
    assert "can't look that up" in out
    assert proposals.staged == []


async def test_egress_executor_fires_the_connector_on_enact() -> None:
    svc = FakeConnectorService()
    node = NodeRow(
        "n",
        None,
        "leaf",
        "egress_call",
        "lbl",
        {"connector": "lookup_medication", "params": {"name": "metformin"}},
        (),
        "approved",
    )
    proposal = ProposalRow("prop-1", "egress", "approved", "health", "t", None)
    await egress_executor(svc)(HEALTH.session, proposal, node)  # type: ignore[arg-type]
    assert svc.fetched == [("lookup_medication", {"name": "metformin"}, "p1")]


class FakeNotes:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_note(self, ctx: object, **kwargs: Any) -> tuple[Any, bool]:
        self.created.append(kwargs)
        return SimpleNamespace(id="n1"), True


class FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, ctx: object, kind: str, payload: dict) -> str:
        self.enqueued.append((kind, payload))
        return "job-1"


class FakeAnalysis:
    def __init__(self) -> None:
        self.merged: list[tuple[str, str]] = []
        self.resolved: list[tuple[str, str, dict]] = []

    async def merge_entities(self, ctx: object, entity_a: str, entity_b: str) -> object:
        self.merged.append((entity_a, entity_b))
        return None

    async def resolve_review(
        self, ctx: object, item_id: str, action: str, payload: dict
    ) -> dict | None:
        self.resolved.append((item_id, action, payload))
        return {"status": "resolved"}


class FakeSkills:
    def __init__(self) -> None:
        self.promoted: list[tuple[str, str]] = []

    async def set_status(self, ctx: object, skill_id: str, status: str) -> None:
        self.promoted.append((skill_id, status))


async def test_leaf_executor_dispatches_by_op() -> None:
    notes, svc, jobs, analysis = FakeNotes(), FakeConnectorService(), FakeJobs(), FakeAnalysis()
    skills = FakeSkills()
    execute = build_leaf_executor(notes, svc, jobs, analysis, skills)  # type: ignore[arg-type]
    proposal = ProposalRow("p", "egress", "approved", "health", "t", None)

    egress_node = NodeRow(
        "e",
        None,
        "leaf",
        "egress_call",
        "",
        {"connector": "lookup_condition", "params": {"name": "x"}},
        (),
        "approved",
    )
    note_node = NodeRow(
        "a", None, "leaf", "add_note", "", {"body": "the fact", "domain": "health"}, (), "approved"
    )
    merge_node = NodeRow(
        "m",
        None,
        "leaf",
        "merge_entities",
        "",
        {"entity_a": "e1", "entity_b": "e2"},
        (),
        "approved",
    )
    skill_node = NodeRow(
        "s", None, "leaf", "skill_promote", "", {"skill_id": "sk1"}, (), "approved"
    )
    predicate_node = NodeRow(
        "pr",
        None,
        "leaf",
        "predicate_resolve",
        "",
        {"card_id": "card-1", "action": "map_to_existing", "canonical_name": "spouse"},
        (),
        "approved",
    )
    await execute(HEALTH.session, proposal, egress_node)
    await execute(HEALTH.session, proposal, note_node)
    await execute(HEALTH.session, proposal, merge_node)
    await execute(HEALTH.session, proposal, skill_node)
    await execute(HEALTH.session, proposal, predicate_node)
    assert svc.fetched == [("lookup_condition", {"name": "x"}, "p1")]
    assert notes.created[0]["provenance"] == "agent"
    # The agent note re-enters ingestion; the egress leaf does not enqueue a note.
    assert jobs.enqueued == [("ingest_note", {"note_id": "n1"})]
    # A merge leaf folds through the analysis repo, not the note path.
    assert analysis.merged == [("e1", "e2")]
    # A skill_promote leaf flips the distilled shadow skill to active.
    assert skills.promoted == [("sk1", "active")]
    # A predicate_resolve leaf applies the card resolution via the shipped resolve_review.
    assert analysis.resolved == [("card-1", "map_to_existing", {"canonical_name": "spouse"})]
