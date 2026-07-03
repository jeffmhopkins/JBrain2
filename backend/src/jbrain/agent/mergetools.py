"""propose_merge — stage a duplicate-entity merge for the owner (docs/reference/ASSISTANT.md
"Staging & approval", invariant #7).

The agent has no privileged write into citable knowledge — it cannot fold two
entities together on its own. `propose_merge` therefore **stages a Proposal**; only
the owner's approval and enact runs the real merge, through the same fold-and-repoint
the review-inbox merge runs (SqlAnalysisRepo.merge_entities), so the two paths can
never diverge. The entity ids ride structurally in the node preview, never in the
prose — so the model can't garble them, and the survivor is chosen at enact time by
the trusted ranking, never by the agent.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from jbrain.agent.contracts import ProposalRef
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.proposals import (
    LeafExecutor,
    NodeRow,
    NodeSpec,
    ProposalRepo,
    ProposalRow,
    ProposalSpec,
)
from jbrain.db.session import SessionContext

if TYPE_CHECKING:
    from jbrain.agent.readtools import EntityReader
    from jbrain.analysis.repo import SqlAnalysisRepo


def build_merge_handlers(proposals: ProposalRepo, entities: EntityReader) -> dict[str, ToolHandler]:
    async def propose_merge_tool(arguments: dict, ctx: ToolContext) -> str:
        a = str(arguments.get("entity_a", "")).strip()
        b = str(arguments.get("entity_b", "")).strip()
        if not a or not b:
            return "propose_merge needs entity_a and entity_b — two entity ids from find_entity."
        if a == b:
            return "those are the same entity id — nothing to merge."
        if not ctx.session.principal_id:
            return "can't stage a merge without an owner principal."
        view_a = await entities.entity_view(ctx.session, a)
        view_b = await entities.entity_view(ctx.session, b)
        if view_a is None or view_b is None:
            return (
                "couldn't find one of those entities in scope — use find_entity to get their ids."
            )
        domain = str(view_a["domain"])
        # You cannot stage a write to a domain the session cannot read.
        if ctx.scopes and domain not in ctx.scopes:
            return f"can't merge into '{domain}' — this session isn't scoped to it."
        name_a, name_b = str(view_a["canonical_name"]), str(view_b["canonical_name"])
        reason = str(arguments.get("reason", "")).strip()
        # The survivor is decided at enact (plan_merge), so the label/title name both
        # entities without asserting a direction. The ids live in the preview, not here.
        node = NodeSpec(
            id=str(uuid.uuid4()),
            type="leaf",
            op="merge_entities",
            label=f"Merge “{name_a}” and “{name_b}”",
            preview={
                "entity_a": a,
                "entity_b": b,
                "name_a": name_a,
                "name_b": name_b,
                "kind_a": str(view_a["kind"]),
                "kind_b": str(view_b["kind"]),
                "domain": domain,
                **({"reason": reason} if reason else {}),
            },
        )
        spec = ProposalSpec(
            kind="merge",
            domain=domain,
            title=f"Merge “{name_a}” and “{name_b}”",
            nodes=[node],
            provenance={"source": "chat"},
            session_id=ctx.agent_session_id,
        )
        prop_id = await proposals.stage(
            ctx.session, principal_id=ctx.session.principal_id, spec=spec
        )
        return ToolOutput(
            f"Staged a merge of “{name_a}” and “{name_b}” for your approval."
            " I won't combine them until you approve — the more-anchored identity survives and"
            " the other's mentions and facts repoint onto it, so nothing is lost.",
            proposal=ProposalRef(proposal_id=prop_id, kind="merge"),
        )

    return {"propose_merge": propose_merge_tool}


def entity_merge_executor(analysis: SqlAnalysisRepo) -> LeafExecutor:
    """Enact a merge_entities leaf: fold one entity into the other through the same
    repo merge the review inbox uses (idempotent on a re-enact — the repo guards an
    already-merged pair)."""

    async def execute(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        if node.op != "merge_entities":
            return
        a = str(node.preview.get("entity_a", ""))
        b = str(node.preview.get("entity_b", ""))
        if a and b:
            await analysis.merge_entities(ctx, a, b)

    return execute
