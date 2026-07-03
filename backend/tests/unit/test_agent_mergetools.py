"""propose_merge STAGES a merge proposal (never folds), keeping the entity ids
structural; the merge leaf executor folds through the analysis repo only on enact
(docs/reference/ASSISTANT.md "Staging & approval", #7)."""

from jbrain.agent.contracts import ProposalRef
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.mergetools import build_merge_handlers, entity_merge_executor
from jbrain.agent.proposals import NodeRow, ProposalRow, ProposalSpec
from jbrain.db.session import SessionContext

CTX = ToolContext(
    session=SessionContext(principal_kind="owner", principal_id="p1", domain_scopes=("general",)),
    scopes=("general",),
)


class FakeProposals:
    def __init__(self) -> None:
        self.staged: list[tuple[str, ProposalSpec]] = []

    async def stage(self, ctx: object, *, principal_id: str, spec: ProposalSpec) -> str:
        self.staged.append((principal_id, spec))
        return "prop-1"


class FakeEntities:
    """Returns a minimal entity_view (the slice propose_merge reads) per id."""

    def __init__(self, views: dict[str, dict | None]) -> None:
        self._views = views

    async def entity_view(self, ctx: object, entity_id: str) -> dict | None:
        return self._views.get(entity_id)


def _view(name: str, kind: str = "Product", domain: str = "general") -> dict:
    return {"canonical_name": name, "kind": kind, "domain": domain}


def handler(proposals: FakeProposals, entities: FakeEntities):
    return build_merge_handlers(proposals, entities)["propose_merge"]  # type: ignore[arg-type]


async def test_propose_merge_stages_a_merge_proposal_with_structural_ids() -> None:
    id_a = "98f10c10-110f-454d-a355-f51d0687adf3"
    id_b = "5b0ac405-846d-42af-9dfe-45bbc5b1f0e1"
    proposals = FakeProposals()
    entities = FakeEntities({id_a: _view("F-150"), id_b: _view("F150")})
    out = await handler(proposals, entities)({"entity_a": id_a, "entity_b": id_b}, CTX)
    assert isinstance(out, ToolOutput)
    assert "Staged a merge" in out
    # The id rides structurally (a "Review proposal" chip), never in the prose.
    assert out.proposal == ProposalRef(proposal_id="prop-1", kind="merge")
    assert "prop-1" not in out
    principal_id, spec = proposals.staged[0]
    assert principal_id == "p1"
    assert spec.kind == "merge" and spec.domain == "general"
    # A clean, name-only title — no raw uuids leaking into human-facing prose.
    assert spec.title == "Merge “F-150” and “F150”"
    assert id_a not in spec.title and id_b not in spec.title
    node = spec.nodes[0]
    assert node.op == "merge_entities"
    assert node.preview["entity_a"] == id_a and node.preview["entity_b"] == id_b
    assert node.preview["name_a"] == "F-150" and node.preview["name_b"] == "F150"


async def test_propose_merge_needs_two_ids() -> None:
    out = await handler(FakeProposals(), FakeEntities({}))({"entity_a": "a"}, CTX)
    assert "needs entity_a and entity_b" in out


async def test_propose_merge_rejects_the_same_entity() -> None:
    proposals = FakeProposals()
    out = await handler(proposals, FakeEntities({}))({"entity_a": "a", "entity_b": "a"}, CTX)
    assert "same entity" in out
    assert proposals.staged == []


async def test_propose_merge_reports_a_missing_entity() -> None:
    proposals = FakeProposals()
    entities = FakeEntities({"a": _view("F-150"), "b": None})
    out = await handler(proposals, entities)({"entity_a": "a", "entity_b": "b"}, CTX)
    assert "couldn't find" in out
    assert proposals.staged == []  # nothing staged when an id doesn't resolve


async def test_propose_merge_refuses_an_out_of_scope_domain() -> None:
    proposals = FakeProposals()
    entities = FakeEntities({"a": _view("Acct", domain="finance"), "b": _view("Acct2", "finance")})
    out = await handler(proposals, entities)({"entity_a": "a", "entity_b": "b"}, CTX)
    assert "isn't scoped" in out
    assert proposals.staged == []  # never stage a write to an unreadable domain


class FakeAnalysis:
    def __init__(self) -> None:
        self.merged: list[tuple[str, str]] = []

    async def merge_entities(self, ctx: object, entity_a: str, entity_b: str) -> object:
        self.merged.append((entity_a, entity_b))
        return None


async def test_entity_merge_executor_folds_on_enact() -> None:
    analysis = FakeAnalysis()
    proposal = ProposalRow("prop-1", "merge", "approved", "general", "t", None)
    node = NodeRow(
        "n",
        None,
        "leaf",
        "merge_entities",
        "lbl",
        {"entity_a": "a", "entity_b": "b"},
        (),
        "approved",
    )
    await entity_merge_executor(analysis)(CTX.session, proposal, node)  # type: ignore[arg-type]
    assert analysis.merged == [("a", "b")]


async def test_entity_merge_executor_ignores_other_ops() -> None:
    analysis = FakeAnalysis()
    proposal = ProposalRow("p", "correction", "approved", "general", "t", None)
    node = NodeRow("n", None, "leaf", "add_note", "lbl", {"body": "x"}, (), "approved")
    await entity_merge_executor(analysis)(CTX.session, proposal, node)  # type: ignore[arg-type]
    assert analysis.merged == []  # only a merge_entities leaf folds
