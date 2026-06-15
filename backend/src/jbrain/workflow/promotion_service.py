"""Wiring the pure promotion gate to the eval-run store (docs/WORKFLOW_ENGINE_PLAN.md
§5 Track C).

`evals.promotion.promotion_decision` is pure: it takes a baseline and a candidate
`EvalRun` and decides. This thin service supplies them from storage — loading the
latest stored baseline run and a candidate run by version label and running the gate
over them — so a self-improvement loop can ask "may this candidate be promoted over
the recorded baseline?" without re-scoring either.

It never flattens the task/safety split: it hands the gate the reconstructed
`EvalRun`s straight from `EvalRunStore`, whose `scores` jsonb preserves both
dimensions. A missing baseline or candidate is fail-closed — no run to compare means
no promotion, the same posture as a missing new-case fixture in the gate itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from evals.promotion import PromotionResult, promotion_decision
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext
from jbrain.workflow.evalstore import EvalRunStore, MalformedEvalRunError


@dataclass(frozen=True)
class PromotionVerdict:
    """The gate's result plus whether both runs were actually found. `decided` is
    False when a run was missing (fail-closed: `result` is None), so a caller never
    reads a missing baseline as an implicit promotion."""

    decided: bool
    result: PromotionResult | None
    reason: str


class PromotionService:
    """Loads stored baseline/candidate runs and runs the pure gate over them."""

    def __init__(self, store: EvalRunStore):
        self._store = store

    @classmethod
    def from_maker(cls, maker: async_sessionmaker[AsyncSession]) -> PromotionService:
        return cls(EvalRunStore(maker))

    async def decide(
        self,
        ctx: SessionContext,
        *,
        suite: str,
        baseline_label: str,
        candidate_label: str,
        new_case: str,
    ) -> PromotionVerdict:
        """Run the promotion gate over the latest stored baseline and candidate for
        `suite`. Fail-closed when either run is absent."""
        try:
            baseline = await self._store.latest(ctx, suite=suite, version_label=baseline_label)
            candidate = await self._store.latest(ctx, suite=suite, version_label=candidate_label)
        except MalformedEvalRunError as exc:
            # A corrupt stored run is fail-closed: don't promote against a run we
            # can't fully trust (a dropped baseline fixture would hide a regression).
            return PromotionVerdict(False, None, f"malformed stored run: {exc}")
        if baseline is None:
            return PromotionVerdict(False, None, f"no stored baseline run for {baseline_label!r}")
        if candidate is None:
            return PromotionVerdict(False, None, f"no stored candidate run for {candidate_label!r}")
        result = promotion_decision(baseline, candidate, new_case=new_case)
        return PromotionVerdict(True, result, result.reason)
