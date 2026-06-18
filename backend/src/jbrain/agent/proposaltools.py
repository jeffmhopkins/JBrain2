"""The propose_correction tool and the agent-note executor (docs/ASSISTANT.md
"Staging & approval", invariant #7).

The agent has no privileged write into citable knowledge. `propose_correction`
therefore **stages a Proposal**, it never writes — the owner enacts it. On
enactment the leaf re-enters as an **agent-authored note** through normal
ingestion: provenance-flagged, source-attributed, NORMAL extraction weight, and
idempotent on its node id so re-enacting can never duplicate it.
"""

import uuid

import structlog

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
from jbrain.agent.skills import SkillsRepo
from jbrain.analysis.repo import AlreadyResolved, SqlAnalysisRepo, UnknownAction
from jbrain.db.session import SessionContext
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import JobEnqueuer

log = structlog.get_logger()

_TITLE_LEN = 80


def _label(text: str, limit: int = _TITLE_LEN) -> str:
    """A short title from prose: truncate on a word boundary with an ellipsis so a
    long correction never gets sliced mid-word (the old `correction[:80]` cut inside
    a word — e.g. "…adf3)a")."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return head.rstrip() + "…"


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
            label=_label(correction),
            preview={"body": correction, "domain": domain},
        )
        spec = ProposalSpec(
            kind="correction",
            domain=domain,
            title=_label(correction),
            nodes=[node],
            provenance={"source": "chat"},
            session_id=ctx.agent_session_id,
        )
        prop_id = await proposals.stage(
            ctx.session, principal_id=ctx.session.principal_id, spec=spec
        )
        # The id rides structurally (a "Review proposal" chip), not in the prose —
        # so the model can't garble it and the user gets a real control.
        return ToolOutput(
            "Staged a correction for your approval. I won't change anything until you approve"
            " it — it then re-enters as a normal, source-attributed note.",
            proposal=ProposalRef(proposal_id=prop_id, kind="correction"),
        )

    return {"propose_correction": propose_correction_tool}


def agent_note_executor(notes: SqlNotesRepo, jobs: JobEnqueuer) -> LeafExecutor:
    """Enact a correction/knowledge leaf as an agent-authored note re-entering the
    ingestion pipeline (#7). Idempotent on the node id, so a re-enact never
    duplicates the note — and it enqueues the same `ingest_note` job a captured
    note does, so it actually indexes and gets analyzed (not stuck at 'pending')."""

    async def execute(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        body = str(node.preview.get("body", "")).strip()
        if not body:
            return
        note, created = await notes.create_note(
            ctx,
            client_id=f"proposal-{node.id}",
            domain=str(node.preview.get("domain") or proposal.domain),
            destination=None,
            body=body,
            provenance="agent",
            source_ref=f"proposal:{proposal.id}",
        )
        # Only a fresh insert needs ingestion; a re-enact is idempotent (already
        # has a note and a job). Without this the note never leaves 'pending'.
        if created:
            await jobs.enqueue(ctx, "ingest_note", {"note_id": note.id})

    return execute


def skill_promotion_executor(skills: SkillsRepo) -> LeafExecutor:
    """Enact a skill-promotion leaf (Loop 2): flip the owner-reviewed shadow skill to `active`,
    so it becomes eligible for turn-time retrieval. Idempotent — set_status is a plain UPDATE, so
    a re-enact is a no-op. The owner approving the proposal IS the trust+promotion gate (the MVP's
    answer to auto-promotion); no auto path writes 'active'."""

    async def execute(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        skill_id = str(node.preview.get("skill_id", "")).strip()
        if not skill_id:
            return
        await skills.set_status(ctx, skill_id, "active")

    return execute


def prompt_edit_executor() -> LeafExecutor:
    """Enact a prompt-edit leaf (Loop 4): RECORD-ONLY. A self-edit is PR-shaped and
    is NEVER runtime-applied (non-neg #6) — the box is air-gapped from git. So enact
    writes NO prompt/tool file, creates NO note, runs NO connector, and changes NO
    runtime behavior; the diff in the proposal preview IS the deliverable, which the
    owner applies as a real PR off-box. This explicit op exists so a prompt-edit leaf
    never falls through to the agent-note executor; the proposal row + its enacted
    status are the record (an audit line, nothing else)."""

    async def execute(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        log.info(
            "prompt_edit_recorded",
            proposal_id=proposal.id,
            node_id=node.id,
            target=node.preview.get("target_name"),
            proposed_version=node.preview.get("proposed_version"),
        )

    return execute


def predicate_resolution_executor(analysis: SqlAnalysisRepo) -> LeafExecutor:
    """Enact a predicate-canon leaf (Loop 3a, Wave 2): apply the owner-approved resolution of a
    `new_predicate` card via the SHIPPED `resolve_review` (map_to_existing / accept_as_new),
    reusing all its committed logic — fact rewrite, mint, the durable alias (Wave 1), the
    consolidate event. The nightly action only STAGES this; owner approval IS the trust gate (no
    auto-resolve). Idempotent: a re-enact of an already-resolved card is a no-op."""

    async def execute(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        card_id = str(node.preview.get("card_id", "")).strip()
        action = str(node.preview.get("action", "")).strip()
        if not card_id or not action:
            return
        payload: dict[str, str] = {}
        canonical = node.preview.get("canonical_name")
        if isinstance(canonical, str) and canonical:
            payload["canonical_name"] = canonical
        try:
            await analysis.resolve_review(ctx, card_id, action, payload)
        except AlreadyResolved:
            return  # a re-enact (or an owner who already resolved it in the UI) — idempotent
        except UnknownAction:
            # The map target was removed between propose and enact (or the card is gone). Skip this
            # one leaf rather than 500-ing the whole domain proposal and blocking its valid leaves;
            # the card stays open and the next sweep re-proposes it with a fresh resolution.
            log.warning("predicate_resolve_skipped", card_id=card_id, action=action)

    return execute
