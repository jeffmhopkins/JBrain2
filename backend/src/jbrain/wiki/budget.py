"""The wiki-build spend gate (docs/PHASE6_WIKI_PLAN.md §3b).

Mirrors `SelfImprovementGate` exactly, but on the SEPARATE wiki-build budget/kill-switch so a
runaway rewrite loop can never starve eval (or interactive) spend. Fail-closed: refuses before
a single token is spent when the kill-switch is on, the day's budget is exhausted, or the next
build's estimate would overrun. `record_spend` meters the real cost after, keyed by UTC date.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jbrain.db.session import SessionContext
from jbrain.settings_store import SqlSettingsStore
from jbrain.workflow.selfimprovement import BudgetDecision


class WikiBudgetExceeded(Exception):
    """Raised when the wiki-build budget/kill-switch refuses a build — the builder stops the run
    (the entity stays dirty, retried next window) rather than spending. Lives here (not in the
    rewriter) so the builder can catch it without importing the rewriter (cycle-free)."""


def _utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()


class WikiBuildGate:
    def __init__(self, settings: SqlSettingsStore):
        self._settings = settings

    async def check(
        self, ctx: SessionContext, *, estimated_tokens: int = 0, now: datetime | None = None
    ) -> BudgetDecision:
        if await self._settings.wiki_build_kill_switch(ctx):
            return BudgetDecision(False, 0, "wiki-build kill-switch is engaged")
        estimated_tokens = max(estimated_tokens, 0)
        day = _utc_day(now)
        budget = await self._settings.wiki_build_daily_budget(ctx)
        spent = await self._settings.wiki_build_spent_today(ctx, day=day)
        remaining = max(budget - spent, 0)
        if remaining <= 0:
            return BudgetDecision(False, 0, f"daily wiki-build budget exhausted ({budget})")
        if estimated_tokens > remaining:
            return BudgetDecision(
                False,
                remaining,
                f"estimated {estimated_tokens} tokens exceeds {remaining} remaining",
            )
        return BudgetDecision(True, remaining, "within wiki-build budget")

    async def record_spend(
        self, ctx: SessionContext, *, tokens: int, now: datetime | None = None
    ) -> None:
        await self._settings.record_wiki_build_spend(ctx, day=_utc_day(now), tokens=max(tokens, 0))
