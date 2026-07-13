"""The propose_correction tool and the agent-note executor (docs/reference/ASSISTANT.md
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


def build_intake_link_handlers(proposals: ProposalRepo) -> dict[str, ToolHandler]:
    """`make_intake_link` (docs/archive/GUIDED_INTAKE_PLAN.md): stages an EDITABLE intake-link
    Proposal, never mints. The owner edits the config and approves; minting (and the
    show-once secret) happens then, via the dedicated mint-from-proposal endpoint — not
    the generic enact, so the secret never has to ride through a leaf executor."""

    async def make_intake_link_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        subject_id = str(arguments.get("subject_id", "")).strip()
        fields_brief = str(arguments.get("fields_brief", "")).strip()
        domain = str(arguments.get("domain", "")).strip() or (
            ctx.scopes[0] if ctx.scopes else "general"
        )
        # subject_id is optional (general intake): omit it for a collection that isn't
        # about a specific existing person (a recipe, general info).
        if not fields_brief:
            return "make_intake_link needs fields_brief — what the interviewer should collect."
        # You cannot stage a link attributed to a domain this session cannot read.
        if ctx.scopes and domain not in ctx.scopes:
            return f"can't stage an intake link in '{domain}' — this session isn't scoped to it."
        if not ctx.session.principal_id:
            return "can't stage an intake link without an owner principal."
        try:
            max_runs = int(arguments.get("max_runs") or 0)
        except (TypeError, ValueError):
            max_runs = 0
        if max_runs < 1:
            return "make_intake_link needs max_runs >= 1 (how many submissions the link accepts)."
        try:
            max_opens = int(arguments.get("max_opens") or 0) or max_runs * 4
        except (TypeError, ValueError):
            max_opens = max_runs * 4
        try:
            ttl_hours = float(arguments.get("ttl_hours") or 0) or 24.0
        except (TypeError, ValueError):
            ttl_hours = 24.0
        config = {
            "subject_id": subject_id or None,
            "domain": domain,
            "fields_brief": fields_brief,
            "persona_brief": str(arguments.get("persona_brief", "")).strip(),
            "opening_blurb": str(arguments.get("opening_blurb", "")).strip(),
            "label": str(arguments.get("label", "")).strip(),
            "max_runs": max_runs,
            "max_opens": max_opens,
            "bind_on_first": bool(arguments.get("bind_on_first", False)),
            "ttl_hours": ttl_hours,
            "capture_enterer_name": bool(arguments.get("capture_enterer_name", True)),
            "disclose_owner_identity": bool(arguments.get("disclose_owner_identity", False)),
        }
        node = NodeSpec(
            id=str(uuid.uuid4()),
            type="leaf",
            op="mint_intake_link",
            label=_label(fields_brief),
            preview=config,
        )
        spec = ProposalSpec(
            kind="intake-link",
            domain=domain,
            subject_id=subject_id or None,
            title=_label(fields_brief),
            nodes=[node],
            provenance={"source": "chat"},
            session_id=ctx.agent_session_id,
        )
        prop_id = await proposals.stage(
            ctx.session, principal_id=ctx.session.principal_id, spec=spec
        )
        return ToolOutput(
            "Staged an intake link for your approval. Edit the details if you like, then"
            " approve to mint it — I'll show you the link once, right after.",
            proposal=ProposalRef(proposal_id=prop_id, kind="intake-link"),
        )

    return {"make_intake_link": make_intake_link_tool}


def agent_note_executor(notes: SqlNotesRepo, jobs: JobEnqueuer) -> LeafExecutor:
    """Enact a correction/knowledge leaf as an agent-authored note re-entering the
    ingestion pipeline (#7). Idempotent on the node id, so a re-enact never
    duplicates the note — and it enqueues the same `ingest_note` job a captured
    note does, so it actually indexes and gets analyzed (not stuck at 'pending')."""

    async def execute(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        body = str(node.preview.get("body", "")).strip()
        if not body:
            return
        # Correct-in-place (INLINE_APPROVALS_PLAN §3.2, Decision #2): when the owner
        # edited the proposed text before approving, the note is the OWNER's correction —
        # provenance='human' with an #edited source_ref, so it carries honest attribution
        # and normal human weight, not the agent's. An un-edited approval stays 'agent'.
        edited = bool(node.preview.get("edited"))
        note, created = await notes.create_note(
            ctx,
            client_id=f"proposal-{node.id}",
            domain=str(node.preview.get("domain") or proposal.domain),
            destination=None,
            body=body,
            provenance="human" if edited else "agent",
            source_ref=f"proposal:{proposal.id}#edited" if edited else f"proposal:{proposal.id}",
        )
        # Only a fresh insert needs ingestion; a re-enact is idempotent (already
        # has a note and a job). Without this the note never leaves 'pending'.
        if created:
            await jobs.enqueue(ctx, "ingest_note", {"note_id": note.id})

    return execute


def intake_note_executor(notes: SqlNotesRepo, jobs: JobEnqueuer) -> LeafExecutor:
    """Enact an approved intake-submission leaf as an `untrusted_origin` attributed note
    (W4, §5). Same re-entry as an agent note — idempotent on the node id, normal-weight
    ingestion via `ingest_note` — but provenance-tagged as stranger-authored so the
    integration backfill drains it behind owner notes. The owner gate is the trust
    boundary; the content is still verbatim stranger text (a documented acceptance)."""

    async def execute(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        body = str(node.preview.get("body", "")).strip()
        if not body:
            return
        submission_id = str(node.preview.get("submission_id", ""))
        note, created = await notes.create_note(
            ctx,
            client_id=f"intake-{node.id}",
            domain=str(node.preview.get("domain") or proposal.domain),
            destination=None,
            body=body,
            provenance="untrusted_origin",
            source_ref=f"intake-submission:{submission_id}",
        )
        if created:
            await jobs.enqueue(ctx, "ingest_note", {"note_id": note.id})

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
