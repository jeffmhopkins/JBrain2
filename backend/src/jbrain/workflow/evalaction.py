"""The `eval_run` action: run the eval suite and store the result as an `EvalRun`
(docs/WORKFLOW_ENGINE_PLAN.md §5 Track C).

This is the one self-improvement action shipped this wave. It is **opt-in** — NOT
one of the six always-on `ACTION_SPECS` in `registry.py`, because those are mirrored
1:1 by the seed rows in migration 0035 (the seed-lockstep test asserts it) and the
worker's boot dispatch validates an exact name match. Adding `eval_run` to the
default six would break that lockstep and need a seed migration. So it lives here as
a separate `EVAL_RUN_SPEC` an operator wires in deliberately, and the seed-row
projection is a deferred follow-up (see the module note below); the in-code spec is
already the source of truth the boot validation keys on.

Two binding properties (E5):
- **Gated.** The handler refuses (fail-closed) before it spends a single token when
  the kill-switch is on or the daily self-improvement budget can't cover it
  (`SelfImprovementGate`), and charges the real cost afterward.
- **Faked in CI.** The actual scoring is a `Scorer` callable injected at wiring time
  — the live one calls a real model through the LLM adapter (never in CI); tests pass
  a fake scorer, exactly like the LLM adapter is faked. The handler itself never
  imports a provider SDK or `evals/run.py`'s live router.

SEED PROJECTION DEFERRED: the `app.actions` reference row for `eval_run` is not
seeded here (no new migration this task, per the track brief). The action runs from
its in-code `EVAL_RUN_SPEC`; the reference projection lands when the nightly-eval
schedule (Track B) wires it as a pipeline step, in that PR's migration.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from jbrain.db.session import SessionContext
from jbrain.settings_store import SqlSettingsStore
from jbrain.workflow.evalstore import EvalRunStore
from jbrain.workflow.promotion import EvalRun
from jbrain.workflow.registry import ActionSpec
from jbrain.workflow.selfimprovement import SelfImprovementGate

# Scores one suite for a version label, returning the run plus the tokens it spent.
# The live implementation drives the eval through the LLM adapter; CI injects a fake
# (no model, deterministic tokens) — the LLM-faked rule applied to a self-edit loop.
Scorer = Callable[[str, str], Awaitable[tuple[EvalRun, int]]]

# Estimated worst-case token cost of one eval run, checked against remaining budget
# BEFORE spending (the gate refuses if it won't fit). Conservative; the real cost is
# recorded after the run from the scorer's reported tokens.
EVAL_RUN_ESTIMATE_TOKENS = 50_000

EVAL_RUN_SPEC = ActionSpec(
    name="eval_run",
    version=1,
    handler="eval_run",
    # The eval reads notes/fixtures but writes only its own audit row; the spend is
    # what the budget meters, hence the expensive class, not the mutating flag.
    domain_optional=True,
    mutating=False,
    cost_class="expensive",
    dedup_key_expr=None,
    description="Run the eval suite (opt-in, budget-gated).",
)


class EvalRunAction:
    """The `eval_run` handler: gate -> score -> store -> charge.

    Bound at wiring time to a `Scorer` (live or fake), the eval-run store, and the
    self-improvement gate. The handler signature is the registry's `Handler`
    (a `dict` payload), so it slots into the worker dispatch like any action.
    """

    def __init__(
        self,
        *,
        scorer: Scorer,
        store: EvalRunStore,
        settings: SqlSettingsStore,
        ctx: SessionContext,
    ):
        self._scorer = scorer
        self._store = store
        self._gate = SelfImprovementGate(settings)
        self._ctx = ctx

    async def run(self, payload: dict[str, Any]) -> None:
        """Run the eval suite named in the payload and store the result, refusing
        (fail-closed) if the budget/kill-switch won't allow the spend.

        Payload: `suite` (required), `version_label` (required), `model` (the model
        label recorded on the run), `new_case` (the fixture the candidate must win,
        optional). A missing required field is a programming error at the call site,
        so it raises rather than silently no-opping."""
        suite = payload["suite"]
        version_label = payload["version_label"]
        model = payload.get("model", "unknown")
        new_case = payload.get("new_case")

        decision = await self._gate.check(self._ctx, estimated_tokens=EVAL_RUN_ESTIMATE_TOKENS)
        if not decision.allowed:
            # Fail-closed refusal: a self-improvement action over budget or behind the
            # kill-switch must NOT spend. PermanentJobError so the queue does not retry
            # (retrying a budget refusal just burns the retry budget for nothing).
            from jbrain.queue import PermanentJobError

            raise PermanentJobError(f"eval_run refused: {decision.reason}")

        run, tokens = await self._scorer(suite, version_label)
        await self._store.save(self._ctx, run, suite=suite, model=model, new_case=new_case)
        await self._gate.record_spend(self._ctx, tokens=tokens)
