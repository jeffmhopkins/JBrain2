"""Owner-side materialization of a captured intake submission into a Proposal (W4).

The recipient's confirmed submission is stranger-authored, untrusted content. The
owner — never the stranger — turns it into a Proposal for review (#4/#10: the capture
itself stages nothing; this is the separate owner step). The submission becomes ONE
note — the whole thing the person provided (a recipe, a set of details) as a single
coherent record — not a shredded pile of per-fact leaves; the note re-enters normal
ingestion, so facts are still extracted from it downstream, once, by the usual pipeline.

Two boundaries make this safe:
  * The transcript is fed to the model behind the strict data/instruction boundary of
    `intake_materialize.prompt` (the `correction_mine` pattern) — the model writes the
    note, it does not take orders from the text.
  * Attribution is CODE-set, not model-set: the Proposal's domain, subject, kind, and
    the note's `untrusted_origin` provenance all come from the OWNER's link config, so
    a poisoned transcript can influence only the note TEXT (which the owner reviews),
    never where the note lands or how trusted it is.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

from jbrain.agent.proposals import NodeSpec, ProposalRepo, ProposalSpec
from jbrain.db.session import SessionContext
from jbrain.intake.service import IntakeRepo
from jbrain.llm import LlmRouter
from jbrain.llm.promptfile import load_prompt

_PROMPT = load_prompt(
    Path(__file__).parent.parent / "agent" / "prompts" / "intake_materialize.prompt"
)
_MATERIALIZE_MAX_TOKENS = 2000


def _transcript_text(transcript: Sequence[dict]) -> str:
    """Render the stored {role,text} turns as a labelled transcript for the model."""
    lines = []
    for entry in transcript:
        who = "Recipient" if entry.get("role") == "recipient" else "Interviewer"
        lines.append(f"{who}: {str(entry.get('text', '')).strip()}")
    return "\n".join(lines)


async def materialize_submission(
    *,
    intake: IntakeRepo,
    proposals: ProposalRepo,
    router: LlmRouter,
    ctx: SessionContext,
    submission_id: str,
) -> str | None:
    """Stage an `intake-submission` Proposal from a captured submission; return its id, or
    None if the submission is unknown or already materialized. Owner context only."""
    submission = await intake.get_submission(ctx, submission_id)
    if submission is None or submission.status != "submitted":
        return None
    link = await intake.get_link(ctx, submission.link_id)
    if link is None:
        return None

    result = await router.complete(
        "intake.materialize",
        system=_PROMPT.body,
        user_text=_transcript_text(submission.transcript or []),
        json_schema=_PROMPT.output_schema,
        max_tokens=_MATERIALIZE_MAX_TOKENS,
        strength=_PROMPT.strength,
    )
    parsed = result.parsed if isinstance(result.parsed, dict) else {}
    title = str(parsed.get("title", "")).strip() or "Intake submission"
    body = str(parsed.get("body", "")).strip()

    # ONE leaf — the whole submission as a single approvable note (a recipe stays a
    # recipe, not four disconnected facts). Attribution (domain, subject, kind,
    # provenance) is set HERE from the link, never from the model output; a submission
    # with nothing usable yields a node-less proposal. On enact the note re-enters
    # ingestion, which extracts its facts the normal way.
    nodes = (
        [
            NodeSpec(
                id=str(uuid.uuid4()),
                type="leaf",
                op="add_intake_note",
                label=title[:80],
                preview={
                    "body": body,
                    "domain": link.domain_code,
                    "submission_id": submission_id,
                },
            )
        ]
        if body
        else []
    )
    spec = ProposalSpec(
        kind="intake-submission",
        domain=link.domain_code,
        subject_id=link.subject_id,
        title=title[:200],
        nodes=nodes,
        provenance={
            "source": "intake-submission",
            "submission_id": submission_id,
            "link_id": submission.link_id,
        },
    )
    proposal_id = await proposals.stage(ctx, principal_id=ctx.principal_id, spec=spec)
    await intake.set_submission_proposal(ctx, submission_id, proposal_id)
    return proposal_id
