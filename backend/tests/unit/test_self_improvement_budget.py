"""The self-improvement spend gate fail-closes on the kill-switch and the daily
token budget (docs/WORKFLOW_ENGINE_PLAN.md E5). A security-adjacent governor — the
refusal paths are exercised exactly, not just the happy path."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jbrain.db.session import SessionContext
from jbrain.settings_store import (
    SELF_IMPROVEMENT_BUDGET_DEFAULT,
    SELF_IMPROVEMENT_BUDGET_KEY,
    SELF_IMPROVEMENT_KILL_SWITCH_KEY,
    SELF_IMPROVEMENT_SPEND_PREFIX,
)
from jbrain.workflow.selfimprovement import SelfImprovementGate

CTX = SessionContext(principal_id="owner", principal_kind="owner")
DAY = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)


class FakeSettings:
    """An in-memory stand-in for SqlSettingsStore: the typed getters the gate calls
    are reused unchanged by composing the real `get`/`upsert` over a dict (so the
    fail-closed coercion in those getters is exercised, not bypassed)."""

    def __init__(self, store: dict[str, Any] | None = None):
        self._store = dict(store or {})

    async def get(self, ctx: SessionContext, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    async def upsert(self, ctx: SessionContext, key: str, value: Any) -> None:
        self._store[key] = value

    # Bind the real typed getters (and the spend recorder) onto the fake backing
    # store — the logic under test, not a re-implementation.
    from jbrain.settings_store import SqlSettingsStore as _S

    self_improvement_kill_switch = _S.self_improvement_kill_switch
    self_improvement_daily_budget = _S.self_improvement_daily_budget
    self_improvement_spent_today = _S.self_improvement_spent_today
    record_self_improvement_spend = _S.record_self_improvement_spend


def _gate(store: dict[str, Any] | None = None) -> tuple[SelfImprovementGate, FakeSettings]:
    settings = FakeSettings(store)
    return SelfImprovementGate(settings), settings  # type: ignore[arg-type]


async def test_allows_within_budget_by_default() -> None:
    gate, _ = _gate()
    decision = await gate.check(CTX, estimated_tokens=1_000, now=DAY)
    assert decision.allowed is True
    assert decision.remaining == SELF_IMPROVEMENT_BUDGET_DEFAULT


async def test_kill_switch_refuses_even_with_full_budget() -> None:
    gate, _ = _gate({SELF_IMPROVEMENT_KILL_SWITCH_KEY: True})
    decision = await gate.check(CTX, estimated_tokens=0, now=DAY)
    assert decision.allowed is False
    assert "kill-switch" in decision.reason


async def test_estimated_cost_over_remaining_is_refused() -> None:
    gate, _ = _gate({SELF_IMPROVEMENT_BUDGET_KEY: 10_000})
    decision = await gate.check(CTX, estimated_tokens=10_001, now=DAY)
    assert decision.allowed is False
    assert "exceeds" in decision.reason


async def test_exhausted_budget_is_refused() -> None:
    spend_key = SELF_IMPROVEMENT_SPEND_PREFIX + DAY.date().isoformat()
    gate, _ = _gate({SELF_IMPROVEMENT_BUDGET_KEY: 5_000, spend_key: 5_000})
    decision = await gate.check(CTX, estimated_tokens=1, now=DAY)
    assert decision.allowed is False
    assert "exhausted" in decision.reason


async def test_recorded_spend_reduces_headroom() -> None:
    gate, settings = _gate({SELF_IMPROVEMENT_BUDGET_KEY: 10_000})
    before = await gate.check(CTX, estimated_tokens=0, now=DAY)
    assert before.remaining == 10_000
    await gate.record_spend(CTX, tokens=4_000, now=DAY)
    after = await gate.check(CTX, estimated_tokens=0, now=DAY)
    assert after.remaining == 6_000
    # Spend is per-UTC-day: a different day starts fresh.
    other_day = datetime(2026, 6, 16, 1, 0, tzinfo=UTC)
    assert (await gate.check(CTX, estimated_tokens=0, now=other_day)).remaining == 10_000


async def test_malformed_budget_falls_back_to_default_not_unlimited() -> None:
    # A junk stored budget must never read as "unlimited" (fail-closed).
    gate, _ = _gate({SELF_IMPROVEMENT_BUDGET_KEY: "lots"})
    decision = await gate.check(CTX, estimated_tokens=0, now=DAY)
    assert decision.remaining == SELF_IMPROVEMENT_BUDGET_DEFAULT
    # A bool is not a valid token count (True == 1 would be a footgun).
    gate2, _ = _gate({SELF_IMPROVEMENT_BUDGET_KEY: True})
    assert (await gate2.check(CTX, now=DAY)).remaining == SELF_IMPROVEMENT_BUDGET_DEFAULT
