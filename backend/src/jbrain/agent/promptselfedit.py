"""The `prompt_self_edit` engine action (Loop 4, Wave 3; docs/LOOP4_PROMPT_TOOL_EDIT_PLAN.md).

The autonomous half of prompt/tool self-editing: nightly, budget-gated, it reads a
**durable, owner-origin signal** — proposals the owner has *rejected*, bucketed by the
internal job (provenance source) that produced them — and when one source's rejections
cross a threshold, drafts a `prompt-edit` Proposal against THAT source's prompt for the
owner to review. Still propose-only: it never applies anything (#6); the owner approves
the diff and lands it as a code change.

Fail-closed and bounded:
- refuses behind the self-improvement kill-switch / budget (#10);
- the signal is the **owner's own rejection decisions** (owner-origin), not untrusted
  content — an untrusted note can't trigger a self-edit (#10);
- the bar holds: it only ever drafts for a prompt that is in `self_editable_targets`
  (so the data-boundary / domain-classification prompts are untargetable, #12);
- a per-prompt cooldown stops it re-nagging about the same prompt every night;
- every draft routes through the SAME `draft_prompt_edit` the owner tool uses, so the
  lint (#9) + version-bump + bar gates can't drift between the two paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.selfedit import self_editable_targets
from jbrain.agent.selfedittools import draft_prompt_edit
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.router import LlmRouter
from jbrain.queue import SYSTEM_CTX, PermanentJobError
from jbrain.settings_store import SqlSettingsStore
from jbrain.workflow.registry import ActionSpec
from jbrain.workflow.selfimprovement import SelfImprovementGate

log = structlog.get_logger()

# The owner-origin signal sources → the prompt whose output they critique. ONLY these
# map; a rejected proposal from any other source (or untrusted origin) is ignored, and
# the target must still be self-editable (the bar), so this can never reach a locked
# prompt. Both prompts are peripheral (owner-judged via proposals), never the firewall.
_SOURCE_TO_PROMPT = {
    "skill_distill": "skill.distill",
    "correction_mine": "correction.mine",
}

_THRESHOLD = 3  # rejected proposals from one source within the window to trigger a draft
_LOOKBACK_DAYS = 30  # only recent rejections count (a stale grudge shouldn't fire forever)
_COOLDOWN_DAYS = 14  # don't re-propose an edit to the same prompt within this window
_PER_DRAFT_ESTIMATE = 8_000  # up-front budget estimate per editable source
_COOLDOWN_KEY = "prompt_self_edit:cooldown"  # {prompt_name: iso_ts of last staged edit}

PROMPT_SELF_EDIT_SPEC = ActionSpec(
    name="prompt_self_edit",
    version=1,
    handler="prompt_self_edit",
    domain_optional=True,
    mutating=True,  # stages proposals
    cost_class="expensive",  # one router call per triggered source
    dedup_key_expr=None,
    description="Draft prompt-edits for prompts whose proposals the owner keeps rejecting.",
)


async def _rejection_clusters(session: AsyncSession, *, sources: list[str], lookback_days: int):
    """Count, per source, the recent proposals the owner REJECTED. A proposal's leaf is
    set 'rejected' by the owner's decision (the proposal row itself stays 'staged'), so
    a rejected leaf is the signal. Owner-origin by construction — these are the owner's
    own decisions, never untrusted content (#10)."""
    rows = (
        await session.execute(
            text(
                "SELECT p.provenance->>'source' AS source, count(DISTINCT p.id) AS cnt"
                " FROM app.proposals p"
                " WHERE p.provenance->>'source' = ANY(:sources)"
                "   AND p.created_at > now() - (:lookback * interval '1 day')"
                "   AND EXISTS (SELECT 1 FROM app.proposal_nodes n"
                "                 WHERE n.proposal_id = p.id AND n.status = 'rejected')"
                " GROUP BY p.provenance->>'source'"
            ),
            {"sources": sources, "lookback": lookback_days},
        )
    ).all()
    return {r.source: int(r.cnt) for r in rows}


async def _owner_principal_id(session: AsyncSession) -> str:
    """The live owner principal uuid a system-staged proposal is attributed to (mirrors
    skilldistill / correctionmine / analysis.persist)."""
    pid = (
        await session.execute(
            text("SELECT id FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL")
        )
    ).scalar_one()
    return str(pid)


class PromptSelfEditAction:
    """gate → count rejection clusters → for each editable, over-threshold, off-cooldown
    source: draft a prompt-edit and stage it for the owner → charge + set cooldown."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        router: LlmRouter,
        settings: SqlSettingsStore,
        proposals: ProposalRepo,
        root: Path | None = None,
        ctx: SessionContext = SYSTEM_CTX,
    ):
        self._maker = maker
        self._router = router
        self._settings = settings
        self._proposals = proposals
        self._root = root
        self._gate = SelfImprovementGate(settings)
        self._ctx = ctx

    async def run(self, _payload: dict[str, Any]) -> None:
        editable = self_editable_targets(self._root)
        # Only sources whose prompt is actually self-editable can ever be drafted — the
        # bar, applied before any spend so a locked/un-opted prompt costs nothing.
        sources = [s for s, p in _SOURCE_TO_PROMPT.items() if p in editable]
        if not sources:
            return
        decision = await self._gate.check(
            self._ctx, estimated_tokens=len(sources) * _PER_DRAFT_ESTIMATE
        )
        if not decision.allowed:
            raise PermanentJobError(f"prompt_self_edit refused: {decision.reason}")

        async with scoped_session(self._maker, self._ctx) as session:
            clusters = await _rejection_clusters(
                session, sources=sources, lookback_days=_LOOKBACK_DAYS
            )
            owner_pid = await _owner_principal_id(session) if clusters else ""

        cooldown = dict(await self._settings.get(self._ctx, _COOLDOWN_KEY, {}) or {})
        now = datetime.now(UTC)
        spent = 0
        for source, count in clusters.items():
            if count < _THRESHOLD:
                continue
            prompt_name = _SOURCE_TO_PROMPT[source]
            if _on_cooldown(cooldown.get(prompt_name), now):
                continue
            target = editable[prompt_name]
            failure_mode = (
                f"The owner rejected {count} of this prompt's proposals in the last"
                f" {_LOOKBACK_DAYS} days — its output is being judged low-value. Revise the"
                " prompt to be more selective and precise so it produces fewer, higher-quality"
                " results, without weakening any existing rule."
            )
            outcome = await draft_prompt_edit(
                self._router, target=target, failure_mode=failure_mode, root=self._root
            )
            spent += outcome.tokens
            if outcome.spec is None:
                log.warning("prompt_self_edit_draft_skipped", prompt=prompt_name, code=outcome.code)
                continue
            await self._proposals.stage(self._ctx, principal_id=owner_pid, spec=outcome.spec)
            cooldown[prompt_name] = now.isoformat()

        await self._settings.upsert(self._ctx, _COOLDOWN_KEY, cooldown)
        if spent:
            await self._gate.record_spend(self._ctx, tokens=spent)


def _on_cooldown(last_iso: Any, now: datetime) -> bool:
    """True if a prompt was last proposed within the cooldown window — don't re-nag."""
    if not isinstance(last_iso, str):
        return False
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return False
    return now - last < timedelta(days=_COOLDOWN_DAYS)


def prompt_self_edit_handler(maker: async_sessionmaker[AsyncSession], *, router: LlmRouter) -> Any:
    """Worker dispatch entry for `prompt_self_edit` (payload-only Handler)."""
    action = PromptSelfEditAction(
        maker,
        router=router,
        settings=SqlSettingsStore(maker),
        proposals=ProposalRepo(maker),
    )
    return action.run
