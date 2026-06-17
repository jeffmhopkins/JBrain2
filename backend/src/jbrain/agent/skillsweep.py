"""The `skill_sweep` engine action (Loop 2, Wave 3; docs/LOOP2_SKILL_LEARNING_PLAN.md).

Nightly hygiene (not a safety gate — owner-gated promotion is the safety gate): enforce a per-domain
ACTIVE-skill cap by usefulness-decay eviction, demoting the least-useful actives back to `shadow`
(reversible — the owner can re-promote; nothing is deleted). "Least useful" = least-recently
surfaced, then least-surfaced; a never-surfaced skill falls back to its creation time so a freshly
promoted one isn't evicted before it can prove out. Quarantined/shadow skills are out of the active
set — neither counted toward the cap nor touched.

Spends no tokens (eviction is pure SQL ranking), but it IS self-improvement work, so the global
kill-switch can halt it (`SelfImprovementGate`, estimated_tokens=0). The cap is a tunable read live
from settings (owner-overridable, fail-closed to a sensible default). The demote runs on an
RLS-scoped session, so it is domain-firewalled like every skills query (non-negotiable #3).
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.skills import SkillsRepo
from jbrain.db.session import SessionContext
from jbrain.queue import SYSTEM_CTX, PermanentJobError
from jbrain.settings_store import SqlSettingsStore
from jbrain.workflow.registry import ActionSpec
from jbrain.workflow.selfimprovement import SelfImprovementGate

log = structlog.get_logger()

SKILL_SWEEP_SPEC = ActionSpec(
    name="skill_sweep",
    version=1,
    handler="skill_sweep",
    domain_optional=True,
    mutating=True,  # demotes active->shadow
    cost_class="cheap",  # pure SQL ranking, no LLM call
    dedup_key_expr=None,
    description="Cap active skills per domain, demoting the least-useful to shadow (reversible).",
)


class SkillSweepAction:
    """gate (kill-switch) → per-domain usefulness-decay eviction (active->shadow)."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        settings: SqlSettingsStore,
        skills: SkillsRepo,
        ctx: SessionContext = SYSTEM_CTX,
    ):
        self._maker = maker
        self._settings = settings
        self._skills = skills
        self._gate = SelfImprovementGate(settings)
        self._ctx = ctx

    async def run(self, _payload: dict[str, Any]) -> None:
        # Spends no tokens, but it IS self-improvement hygiene, so the global kill-switch must be
        # able to halt it. estimated_tokens=0 → only the kill-switch / exhausted-budget terms gate;
        # nothing is charged (no record_spend).
        decision = await self._gate.check(self._ctx, estimated_tokens=0)
        if not decision.allowed:
            raise PermanentJobError(f"skill_sweep refused: {decision.reason}")

        cap = await self._settings.skill_active_cap(self._ctx)
        demoted = await self._skills.demote_over_cap(self._ctx, cap)
        if demoted:
            log.info("skill_sweep_evicted", count=len(demoted), cap=cap)


def skill_sweep_handler(maker: async_sessionmaker[AsyncSession]) -> Any:
    """Worker dispatch entry for `skill_sweep` (payload-only Handler)."""
    action = SkillSweepAction(
        maker,
        settings=SqlSettingsStore(maker),
        skills=SkillsRepo(maker),
    )
    return action.run
