"""The propose_correction tool and the agent-note executor (docs/ASSISTANT.md
"Staging & approval", invariant #7).

The agent has no privileged write into citable knowledge. `propose_correction`
therefore **stages a Proposal**, it never writes — the owner enacts it. On
enactment the leaf re-enters as an **agent-authored note** through normal
ingestion: provenance-flagged, source-attributed, NORMAL extraction weight, and
idempotent on its node id so re-enacting can never duplicate it.
"""

import uuid

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.agent.proposals import (
    LeafExecutor,
    NodeRow,
    NodeSpec,
    ProposalRepo,
    ProposalRow,
    ProposalSpec,
)
from jbrain.db.session import SessionContext
from jbrain.notes.repo import SqlNotesRepo

_TITLE_LEN = 80


def build_proposal_handlers(proposals: ProposalRepo) -> dict[str, ToolHandler]:
    async def propose_correction_tool(arguments: dict, ctx: ToolContext) -> str:
        correction = str(arguments.get("correction", "")).strip()
        if not correction:
            return "propose_correction needs the correction text."
        domain = str(arguments.get("domain", "")).strip() or (
            ctx.scopes[0] if ctx.scopes else "general"
        )
        # You cannot stage a write to a domain the session cannot read.
        if ctx.scopes and domain not in ctx.scopes:
            return f"can't stage a correction in '{domain}' — this session isn't scoped to it."
        if not ctx.session.principal_id:
            return "can't stage a correction without an owner principal."
        node = NodeSpec(
            id=str(uuid.uuid4()),
            type="leaf",
            op="add_note",
            label=correction[:_TITLE_LEN],
            preview={"body": correction, "domain": domain},
        )
        spec = ProposalSpec(
            kind="correction",
            domain=domain,
            title=correction[:_TITLE_LEN],
            nodes=[node],
            provenance={"source": "chat"},
        )
        prop_id = await proposals.stage(
            ctx.session, principal_id=ctx.session.principal_id, spec=spec
        )
        return (
            f"Staged a correction for your approval (proposal {prop_id}). I won't change anything"
            " until you approve it — it then re-enters as a normal, source-attributed note."
        )

    return {"propose_correction": propose_correction_tool}


def agent_note_executor(notes: SqlNotesRepo) -> LeafExecutor:
    """Enact a correction/knowledge leaf as an agent-authored note re-entering the
    ingestion pipeline (#7). Idempotent on the node id, so a re-enact never
    duplicates the note."""

    async def execute(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        body = str(node.preview.get("body", "")).strip()
        if not body:
            return
        await notes.create_note(
            ctx,
            client_id=f"proposal-{node.id}",
            domain=str(node.preview.get("domain") or proposal.domain),
            destination=None,
            body=body,
            provenance="agent",
            source_ref=f"proposal:{proposal.id}",
        )

    return execute
