"""The eval_run action (gated + LLM-faked) and the promotion service wiring
(docs/WORKFLOW_ENGINE_PLAN.md §5 Track C).

The store and the gate's settings are faked in-memory (no DB, no model) — the
round-trip against real Postgres lives in tests/integration. Here the focus is the
wiring: the gate refuses before spending, a fake scorer stands in for the live model,
and the promotion service reconstructs candidate<->baseline from stored runs without
flattening the task/safety split."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from jbrain.db.session import SessionContext
from jbrain.queue import PermanentJobError
from jbrain.settings_store import (
    SELF_IMPROVEMENT_BUDGET_KEY,
    SELF_IMPROVEMENT_KILL_SWITCH_KEY,
)
from jbrain.workflow.evalaction import EVAL_RUN_SPEC, EvalRunAction
from jbrain.workflow.promotion import EvalRun, FixtureScore
from jbrain.workflow.promotion_service import PromotionService
from tests.unit.test_self_improvement_budget import FakeSettings

CTX = SessionContext(principal_id="owner", principal_kind="owner")


def test_run_from_scores_rejects_malformed_rows_fail_closed() -> None:
    """A corrupt stored `scores` value must RAISE, not reconstruct a partial run: a
    dropped baseline fixture would fail open (an uncompared regression). E5 / 100%
    security-path coverage."""
    from jbrain.workflow.evalstore import MalformedEvalRunError, _run_from_scores

    # well-formed round-trips
    ok = _run_from_scores("v1", [{"fixture": "a", "task": 1.0, "safety": 1.0}])
    assert ok.scores == (FixtureScore("a", 1.0, 1.0),)
    assert _run_from_scores("v1", []).scores == ()

    # every malformed shape is rejected, not silently dropped
    for bad in (
        {"not": "a list"},  # not a list
        "string",  # not a list
        [{"fixture": "a", "task": 1.0}],  # missing safety
        [{"fixture": "a", "safety": 1.0}],  # missing task
        [{"task": 1.0, "safety": 1.0}],  # missing fixture name
        [{"fixture": "a", "task": True, "safety": 1.0}],  # bool is not a score
        [{"fixture": "a", "task": 1.0, "safety": "x"}],  # non-numeric
        [{"fixture": "a", "task": 1.0, "safety": 1.0}, "junk"],  # one bad item
    ):
        with pytest.raises(MalformedEvalRunError):
            _run_from_scores("v1", bad)


class FakeEvalStore:
    """In-memory EvalRunStore: `save` appends, `latest` returns the most recent run
    for a (suite, version_label) — the storage contract the service/action depend on,
    without Postgres."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def save(
        self,
        ctx: SessionContext,
        run: EvalRun,
        *,
        suite: str,
        model: str,
        new_case: str | None = None,
    ) -> str:
        self.rows.append(
            {"suite": suite, "label": run.version, "model": model, "run": run, "new_case": new_case}
        )
        return f"id-{len(self.rows)}"

    async def latest(
        self, ctx: SessionContext, *, suite: str, version_label: str
    ) -> EvalRun | None:
        for row in reversed(self.rows):
            if row["suite"] == suite and row["label"] == version_label:
                return row["run"]
        return None


def _run(version: str, *scores: tuple[str, float, float]) -> EvalRun:
    return EvalRun(version, tuple(FixtureScore(f, t, s) for f, t, s in scores))


# --- the eval_run action ---------------------------------------------------------


def _action(store: FakeEvalStore, settings: FakeSettings, scorer: Any) -> EvalRunAction:
    return EvalRunAction(scorer=scorer, store=store, settings=settings, ctx=CTX)  # type: ignore[arg-type]


async def test_eval_run_stores_and_charges_when_within_budget() -> None:
    store, settings = FakeEvalStore(), FakeSettings({SELF_IMPROVEMENT_BUDGET_KEY: 100_000})
    scored = _run("cand", ("a", 1.0, 1.0))

    async def fake_scorer(suite: str, label: str) -> tuple[EvalRun, int]:
        return scored, 20_000

    action = _action(store, settings, fake_scorer)
    await action.run({"suite": "integration", "version_label": "cand", "model": "fake"})

    assert store.rows[0]["run"] is scored
    assert store.rows[0]["model"] == "fake"
    # The real cost was charged against today's (UTC) budget — the handler keys the
    # spend on the actual clock, so read the same day back.
    today = datetime.now(UTC).date().isoformat()
    spent = await settings.self_improvement_spent_today(  # type: ignore[attr-defined]
        CTX, day=today
    )
    assert spent == 20_000


async def test_eval_run_refuses_under_kill_switch_without_scoring() -> None:
    store = FakeEvalStore()
    settings = FakeSettings({SELF_IMPROVEMENT_KILL_SWITCH_KEY: True})
    called = False

    async def scorer(suite: str, label: str) -> tuple[EvalRun, int]:
        nonlocal called
        called = True
        return _run("x"), 1

    action = _action(store, settings, scorer)
    with pytest.raises(PermanentJobError, match="kill-switch"):
        await action.run({"suite": "s", "version_label": "v"})
    assert called is False  # fail-closed: never spent
    assert store.rows == []


async def test_eval_run_refuses_when_estimate_overruns_budget() -> None:
    store = FakeEvalStore()
    settings = FakeSettings({SELF_IMPROVEMENT_BUDGET_KEY: 10})  # less than the estimate

    async def scorer(suite: str, label: str) -> tuple[EvalRun, int]:  # pragma: no cover
        raise AssertionError("must not score when over budget")

    action = _action(store, settings, scorer)
    with pytest.raises(PermanentJobError):
        await action.run({"suite": "s", "version_label": "v"})


def test_eval_run_spec_is_opt_in_not_a_shipped_action() -> None:
    # It is intentionally NOT one of the six always-on ACTION_SPECS (seed lockstep).
    from jbrain.workflow.registry import ACTION_SPECS

    assert EVAL_RUN_SPEC.name == "eval_run"
    assert EVAL_RUN_SPEC.name not in {s.name for s in ACTION_SPECS}


# --- the promotion service -------------------------------------------------------


async def test_promotion_service_decides_from_stored_runs() -> None:
    store = FakeEvalStore()
    service = PromotionService(store)  # type: ignore[arg-type]
    await store.save(CTX, _run("baseline", ("a", 1.0, 1.0), ("b", 1.0, 1.0)), suite="s", model="m")
    await store.save(
        CTX,
        _run("cand", ("a", 1.0, 1.0), ("b", 1.0, 1.0), ("c", 1.0, 1.0)),
        suite="s",
        model="m",
    )
    verdict = await service.decide(
        CTX, suite="s", baseline_label="baseline", candidate_label="cand", new_case="c"
    )
    assert verdict.decided is True
    assert verdict.result is not None and verdict.result.promote is True


async def test_promotion_service_preserves_safety_split() -> None:
    # A candidate that gains task but erodes safety must be blocked — proves the
    # service did NOT flatten the two dimensions in storage.
    store = FakeEvalStore()
    service = PromotionService(store)  # type: ignore[arg-type]
    await store.save(CTX, _run("base", ("a", 0.5, 1.0)), suite="s", model="m")
    await store.save(CTX, _run("cand", ("a", 1.0, 0.4), ("c", 1.0, 1.0)), suite="s", model="m")
    verdict = await service.decide(
        CTX, suite="s", baseline_label="base", candidate_label="cand", new_case="c"
    )
    assert verdict.result is not None and verdict.result.promote is False
    assert verdict.result.safety_regressions == ("a",)


async def test_promotion_service_fail_closed_when_a_run_is_missing() -> None:
    store = FakeEvalStore()
    service = PromotionService(store)  # type: ignore[arg-type]
    await store.save(CTX, _run("base", ("a", 1.0, 1.0)), suite="s", model="m")
    verdict = await service.decide(
        CTX, suite="s", baseline_label="base", candidate_label="missing", new_case="c"
    )
    assert verdict.decided is False
    assert verdict.result is None
    assert "candidate" in verdict.reason
