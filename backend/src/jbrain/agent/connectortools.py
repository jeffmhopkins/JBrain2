"""The connector tools and the egress leaf executor (docs/reference/ASSISTANT.md "External
connectors", invariant #9).

A connector is the `external` permission class, gated by the Proposal primitive: a
connector tool NEVER calls out. It egress-guards the request (typed slots only) and
**stages an egress Proposal whose preview is the exact outbound payload** — the
owner approves what leaves the box before it leaves. Only enacting that Proposal
runs the call (the egress leaf executor), which fetches server-side, caches, and
logs. The leaf executor dispatches by op, so one executor serves both the
agent-note kinds (correction/knowledge) and egress.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.agent.mergetools import entity_merge_executor
from jbrain.agent.proposals import (
    LeafExecutor,
    NodeRow,
    NodeSpec,
    ProposalRepo,
    ProposalRow,
    ProposalSpec,
)
from jbrain.agent.proposaltools import (
    agent_note_executor,
    intake_note_executor,
    predicate_resolution_executor,
)
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.connectors.base import ConnectorRegistry, EgressGuardError, build_egress
from jbrain.connectors.service import ConnectorService
from jbrain.db.session import SessionContext
from jbrain.external.corpus import delete_external_video
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import JobEnqueuer


def build_connector_handlers(
    registry: ConnectorRegistry, proposals: ProposalRepo
) -> dict[str, ToolHandler]:
    """One handler per enabled connector — each stages an egress Proposal rather
    than calling out."""
    return {name: _handler(name, registry, proposals) for name in registry.names()}


def _handler(name: str, registry: ConnectorRegistry, proposals: ProposalRepo) -> ToolHandler:
    async def connector_tool(arguments: dict, ctx: ToolContext) -> str:
        connector = registry.get(name)
        if ctx.scopes and connector.domain not in ctx.scopes:
            return f"can't look that up — this session isn't scoped to {connector.domain}."
        if not ctx.session.principal_id:
            return "can't stage an off-box lookup without an owner principal."
        # Fill only the connector's declared slots from the tool args (the guard
        # rejects anything else, so conversation context can't ride along).
        params = {
            spec.name: arguments[spec.name] for spec in connector.params if spec.name in arguments
        }
        try:
            request = build_egress(connector, params)
        except EgressGuardError as exc:
            return f"can't look that up: {exc}"
        node = NodeSpec(
            id=str(uuid.uuid4()),
            type="leaf",
            op="egress_call",
            label=f"{name} {request.query}",
            preview={
                "connector": name,
                "params": params,
                "url": request.url,
                "query": request.query,
            },
        )
        spec = ProposalSpec(
            kind="egress",
            domain=connector.domain,
            title=f"{name} → {request.url}",
            nodes=[node],
            provenance={"source": "chat"},
            session_id=ctx.agent_session_id,
        )
        prop_id = await proposals.stage(
            ctx.session, principal_id=ctx.session.principal_id, spec=spec
        )
        return (
            f"That needs an off-box lookup, which I won't make on my own. I've staged it for your"
            f" approval (proposal {prop_id}) — it calls {request.url} with {request.query}, and"
            " nothing leaves the box until you approve."
        )

    return connector_tool


def egress_executor(service: ConnectorService) -> LeafExecutor:
    """Enact an egress leaf: the one place the off-box call fires, on approval."""

    async def execute(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        if node.op != "egress_call":
            return
        connector = str(node.preview.get("connector", ""))
        params = dict(node.preview.get("params") or {})
        await service.fetch(
            ctx,
            connector_name=connector,
            params=params,
            principal_id=ctx.principal_id or "",
        )

    return execute


def build_leaf_executor(
    notes: SqlNotesRepo,
    connectors: ConnectorService,
    jobs: JobEnqueuer,
    analysis: SqlAnalysisRepo,
    maker: async_sessionmaker[AsyncSession],
) -> LeafExecutor:
    """The Proposal executor, dispatching by leaf op: an egress_call fires the
    connector; a merge_entities leaf folds one entity into another through the
    analysis repo; a delete_external_video leaf hard-deletes one library video; everything
    else (correction/knowledge, and a manage_appointment change) re-enters as an agent note
    from its preview `body` (which enqueues ingestion via `jobs`) — so an approved appointment
    flows through extraction to the projection like any note."""
    note_executor = agent_note_executor(notes, jobs)
    egress = egress_executor(connectors)
    merge = entity_merge_executor(analysis)
    predicate_resolve = predicate_resolution_executor(analysis)
    intake_note = intake_note_executor(notes, jobs)

    async def execute(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        if node.op == "egress_call":
            await egress(ctx, proposal, node)
        elif node.op == "merge_entities":
            await merge(ctx, proposal, node)
        elif node.op == "predicate_resolve":
            await predicate_resolve(ctx, proposal, node)
        elif node.op == "add_intake_note":
            await intake_note(ctx, proposal, node)
        elif node.op == "delete_external_video":
            # The owner approved removing a library video; the trusted executor hard-deletes it
            # (chunks cascade). `source_id` was fixed by the agent's scope-checked staging.
            await delete_external_video(maker, ctx, str(node.preview.get("source_id", "")))
        elif node.op == "mint_intake_link":
            # No-op here: an intake-link Proposal is minted via the dedicated
            # mint-from-proposal endpoint (it surfaces the show-once secret, which a
            # leaf executor can't return). The generic enact must NOT fall through to
            # the note executor and turn the config into a note.
            return
        else:
            await note_executor(ctx, proposal, node)

    return execute
