"""The self-improvement spend gate (docs/WORKFLOW_ENGINE_PLAN.md E5, I-10).

Any pipeline that makes LLM calls for self-improvement (eval runs today, future
distillation) must pass through `SelfImprovementGate.check` before it spends. The
gate is fail-closed on two terms read live from the settings store: a global
kill-switch (one flip refuses everything) and a SEPARATE per-day token budget (so a
runaway loop can never starve interactive spend). It refuses when the kill-switch is
on, when the budget is already exhausted, or when the next action's estimated cost
would overrun the day's remaining tokens.

The budget is metered, not just gated: `record_spend` adds an action's real token
cost to the day's tally after it runs, so the next `check` sees less headroom. The
day key is UTC so a fixed clock controls it in tests (no wall-clock dependency).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from jbrain.db.session import SessionContext
from jbrain.settings_store import SqlSettingsStore


def _utc_day(now: datetime | None = None) -> str:
    """The UTC calendar date the budget is keyed on. Injectable `now` so a test
    pins the day deterministically (the budget is a daily window, N3-style)."""
    moment = now or datetime.now(UTC)
    return moment.astimezone(UTC).date().isoformat()


@dataclass(frozen=True)
class BudgetDecision:
    """Whether a self-improvement action may run, plus the headroom that informed
    it. `allowed` is False whenever the kill-switch is on or the estimated cost
    would overrun the day's remaining budget."""

    allowed: bool
    remaining: int
    reason: str


class SelfImprovementGate:
    """Reads the kill-switch + daily budget live and decides whether a self-
    improvement action may spend."""

    def __init__(self, settings: SqlSettingsStore):
        self._settings = settings

    async def check(
        self,
        ctx: SessionContext,
        *,
        estimated_tokens: int = 0,
        now: datetime | None = None,
    ) -> BudgetDecision:
        """Fail-closed gate before a self-improvement action runs. Refuses when the
        kill-switch is engaged, the day's budget is already spent, or
        `estimated_tokens` would overrun the remaining headroom."""
        if await self._settings.self_improvement_kill_switch(ctx):
            return BudgetDecision(False, 0, "self-improvement kill-switch is engaged")
        # A negative estimate can't overrun; clamp it so a bad caller can't use one to
        # skip the headroom check (defensive, matching record_spend's clamp).
        estimated_tokens = max(estimated_tokens, 0)
        day = _utc_day(now)
        budget = await self._settings.self_improvement_daily_budget(ctx)
        spent = await self._settings.self_improvement_spent_today(ctx, day=day)
        remaining = max(budget - spent, 0)
        if remaining <= 0:
            return BudgetDecision(False, 0, f"daily self-improvement budget exhausted ({budget})")
        if estimated_tokens > remaining:
            return BudgetDecision(
                False,
                remaining,
                f"estimated {estimated_tokens} tokens exceeds {remaining} remaining today",
            )
        return BudgetDecision(True, remaining, "within self-improvement budget")

    async def record_spend(
        self, ctx: SessionContext, *, tokens: int, now: datetime | None = None
    ) -> None:
        """Charge `tokens` to today's tally after an action ran — the next `check`
        sees the reduced headroom."""
        await self._settings.record_self_improvement_spend(ctx, day=_utc_day(now), tokens=tokens)
