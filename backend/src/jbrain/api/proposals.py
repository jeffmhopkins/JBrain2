"""The Proposals API — the unified review-inbox surface for agent-staged work
(docs/reference/ASSISTANT.md "Staging & approval"). Owner-only.

List the open proposals, open a tree, approve/reject a node (cascading by
containment), and enact — which runs every approved leaf whose prerequisites are
satisfied through the agent-note executor, holding the rest. The agent's authority
never changes: each approval authorises one bounded operation, run by the trusted
executor under the owner's hand.
"""

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jbrain.agent.connectortools import build_leaf_executor
from jbrain.agent.proposals import ProposalRepo, enact_outcome_summary
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.api.deps import owner_only
from jbrain.api.notes import ctx_for
from jbrain.auth.service import PrincipalInfo
from jbrain.connectors.service import ConnectorService
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import JobEnqueuer

router = APIRouter(prefix="/proposals", dependencies=[Depends(owner_only)])

OwnerDep = Annotated[PrincipalInfo, Depends(owner_only)]


def get_proposals(request: Request) -> ProposalRepo:
    return cast(ProposalRepo, request.app.state.agent_proposals)


def get_notes_repo(request: Request) -> SqlNotesRepo:
    return cast(SqlNotesRepo, request.app.state.notes_repo)


def get_connector_service(request: Request) -> ConnectorService:
    return cast(ConnectorService, request.app.state.connector_service)


def get_job_queue(request: Request) -> JobEnqueuer:
    return cast(JobEnqueuer, request.app.state.job_queue)


def get_analysis_repo(request: Request) -> SqlAnalysisRepo:
    return cast(SqlAnalysisRepo, request.app.state.analysis_repo)


class ProposalSummaryOut(BaseModel):
    id: str
    kind: str
    status: str
    domain: str
    title: str
    node_count: int


class NodeOut(BaseModel):
    id: str
    parent_id: str | None
    type: str
    op: str
    label: str
    preview: dict[str, Any]
    deps: list[str]
    status: str


class ProposalOut(BaseModel):
    id: str
    kind: str
    status: str
    domain: str
    title: str
    nodes: list[NodeOut]


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    # Owner-eyes reason for a decline (ignored on approve) — folded into the enact
    # outcome the assistant sees, so it learns *why*, not just that you said no.
    reason: str | None = None


class EditIn(BaseModel):
    # The owner's corrected text for a staged note/appointment leaf (correct-in-place).
    body: str


class EnactOut(BaseModel):
    enacted: list[str]
    held: list[str]
    # A server-authored summary of what this enact did (INLINE_APPROVALS_PLAN §3.1) —
    # the honest, DB-derived artefact the PWA sends back to the assistant so it follows
    # up. "" when nothing ran.
    outcome: str = ""


@router.get("")
async def list_proposals(
    request: Request, principal: OwnerDep, session_id: str | None = None
) -> list[ProposalSummaryOut]:
    # `session_id` scopes the inbox to a Full Brain chat: its own staged proposals
    # plus the session-less background/system ones (so the owner never loses sight
    # of nightly work). Omit it for the unscoped, see-everything list.
    repo = get_proposals(request)
    summaries = await repo.list_open(ctx_for(principal), session_id)
    return [ProposalSummaryOut(**vars(s)) for s in summaries]


@router.get("/{proposal_id}")
async def get_proposal(request: Request, principal: OwnerDep, proposal_id: str) -> ProposalOut:
    repo = get_proposals(request)
    try:
        proposal, nodes = await repo.load(ctx_for(principal), proposal_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ProposalOut(
        id=proposal.id,
        kind=proposal.kind,
        status=proposal.status,
        domain=proposal.domain,
        title=proposal.title,
        nodes=[
            NodeOut(
                id=n.id,
                parent_id=n.parent_id,
                type=n.type,
                op=n.op,
                label=n.label,
                preview=n.preview,
                deps=list(n.deps),
                status=n.status,
            )
            for n in nodes
        ],
    )


@router.post("/{proposal_id}/nodes/{node_id}/decision", status_code=204)
async def decide_node(
    request: Request, principal: OwnerDep, proposal_id: str, node_id: str, body: DecisionIn
) -> None:
    repo = get_proposals(request)
    try:
        await repo.decide(
            ctx_for(principal), node_id, approve=body.decision == "approve", reason=body.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{proposal_id}/nodes/{node_id}/edit", status_code=204)
async def edit_node(
    request: Request, principal: OwnerDep, proposal_id: str, node_id: str, body: EditIn
) -> None:
    """Correct-in-place: replace a staged note/appointment leaf's proposed text before
    approval. The edit files as the owner's correction at enact (provenance='human');
    firewall fields are untouched. 404 if the node isn't an editable, still-staged leaf."""
    repo = get_proposals(request)
    if not await repo.patch_node_body(ctx_for(principal), node_id, body.body):
        raise HTTPException(status_code=404, detail="no editable staged node with that id in scope")


@router.post("/{proposal_id}/enact")
async def enact_proposal(request: Request, principal: OwnerDep, proposal_id: str) -> EnactOut:
    repo = get_proposals(request)
    # One executor dispatching by leaf op: agent-note kinds re-enter the pipeline;
    # an egress leaf fires its connector (the call the owner just approved).
    executor = build_leaf_executor(
        get_notes_repo(request),
        get_connector_service(request),
        get_job_queue(request),
        get_analysis_repo(request),
    )
    ctx = ctx_for(principal)
    try:
        plan = await repo.enact(ctx, proposal_id, executor)
        # Re-load the settled tree (fresh statuses + decision notes + edited flags) to
        # author the outcome the PWA sends back to the assistant — DB truth, not model
        # text. The load is RLS-scoped exactly like the enact, so an out-of-scope id is
        # already a 404 above.
        proposal, nodes = await repo.load(ctx, proposal_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return EnactOut(
        enacted=list(plan.enactable),
        held=list(plan.held),
        outcome=enact_outcome_summary(proposal, nodes, plan),
    )
