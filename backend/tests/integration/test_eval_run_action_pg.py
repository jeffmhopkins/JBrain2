"""The `eval_run` action end-to-end through the worker against real Postgres
(Phase-5 Track H·A). An enqueued `eval_run` job is claimed by `worker.process_one`,
runs behind the self-improvement budget gate, and — with a FAKE scorer (no model,
deterministic tokens, the LLM-faked rule applied to a self-edit loop) — stores an
`EvalRun` with the `{task, safety}` split intact and charges the budget.

The fail-closed refusal path is security-adjacent (E5): a kill-switched / budget-
exhausted job must spend NOTHING, store NOTHING, and NOT retry (`PermanentJobError`
→ the job lands `failed` permanently, not re-queued). Both directions are asserted
here, on real RLS, so the gate is proven end-to-end and not just in the unit wiring.

`eval_run` is deliberately registered in-code only (the worker composes
`EVAL_RUN_SPEC` into the registry like `PURGE_ACTION`); it is NOT in `ACTION_SPECS`
nor the `app.actions` seed — the 0035 seed-lockstep test still asserts the six-row
set. That projection + a nightly schedule are the deferred H·B follow-up.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain import queue, worker
from jbrain.settings_store import (
    SELF_IMPROVEMENT_BUDGET_KEY,
    SELF_IMPROVEMENT_KILL_SWITCH_KEY,
    SELF_IMPROVEMENT_SPEND_PREFIX,
    SqlSettingsStore,
)
from jbrain.workflow.eval_scorer import eval_run_handler
from jbrain.workflow.evalstore import EvalRunStore
from jbrain.workflow.promotion import EvalRun, FixtureScore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_SCORED_TOKENS = 12_345


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _fake_scorer(run: EvalRun):
    """A deterministic scorer (no model): returns a fixed run + a fixed token cost,
    and records whether it was reached so a refusal can assert it was NOT."""
    calls: list[tuple[str, str]] = []

    async def scorer(suite: str, version_label: str) -> tuple[EvalRun, int]:
        calls.append((suite, version_label))
        return run, _SCORED_TOKENS

    return scorer, calls


async def _job_status(maker: async_sessionmaker, job_id: str) -> tuple[str, int]:
    # app.jobs is owner-only; read it under SYSTEM_CTX, the same scope the worker
    # writes it under.
    from jbrain.db.session import scoped_session

    async with scoped_session(maker, queue.SYSTEM_CTX) as sess:
        row = (
            await sess.execute(
                text("SELECT status, attempts FROM app.jobs WHERE id = :id"),
                {"id": job_id},
            )
        ).first()
    assert row is not None
    return row.status, row.attempts


async def _spent_today(maker: async_sessionmaker) -> int:
    from datetime import UTC, datetime

    settings = SqlSettingsStore(maker)
    day = datetime.now(UTC).date().isoformat()
    return await settings.self_improvement_spent_today(queue.SYSTEM_CTX, day=day)


async def test_eval_run_job_scores_stores_and_charges(maker: async_sessionmaker) -> None:
    # A within-budget run stores the candidate (task/safety split intact) and charges
    # the real reported cost against today's UTC budget.
    # Set an explicit ample budget + clear the kill-switch: this DB is shared across
    # tests in the suite, so don't rely on the default surviving a refusal test's
    # teardown.
    settings = SqlSettingsStore(maker)
    await settings.upsert(queue.SYSTEM_CTX, SELF_IMPROVEMENT_KILL_SWITCH_KEY, False)
    await settings.upsert(queue.SYSTEM_CTX, SELF_IMPROVEMENT_BUDGET_KEY, 10_000_000)

    suite = f"suite-{uuid.uuid4().hex[:8]}"
    run = EvalRun(suite, (FixtureScore("alpha", 0.75, 1.0), FixtureScore("beta", 1.0, 0.5)))
    scorer, calls = _fake_scorer(run)
    handler = eval_run_handler(maker, scorer)

    before = await _spent_today(maker)
    job_id = await queue.enqueue(
        maker,
        queue.SYSTEM_CTX,
        "eval_run",
        {"suite": suite, "version_label": suite, "model": "fake-model", "new_case": "beta"},
    )
    assert await worker.process_one(maker, {"eval_run": handler}) is True

    status, _attempts = await _job_status(maker, job_id)
    assert status == "done"
    assert calls == [(suite, suite)]  # the scorer ran exactly once

    loaded = await EvalRunStore(maker).latest(OWNER, suite=suite, version_label=suite)
    assert loaded is not None
    by = loaded.by_fixture()
    assert by["alpha"] == FixtureScore("alpha", 0.75, 1.0)
    assert by["beta"] == FixtureScore("beta", 1.0, 0.5)  # the safety dimension survived

    assert await _spent_today(maker) == before + _SCORED_TOKENS


async def test_eval_run_job_refuses_fail_closed_under_kill_switch(
    maker: async_sessionmaker,
) -> None:
    # Security-adjacent (E5): the kill-switch refuses BEFORE any spend. The scorer is
    # never reached, nothing is stored, no token is charged, and the job is failed
    # PERMANENTLY (not re-queued — retrying a budget refusal burns retries for nothing).
    settings = SqlSettingsStore(maker)
    await settings.upsert(queue.SYSTEM_CTX, SELF_IMPROVEMENT_KILL_SWITCH_KEY, True)
    suite = f"suite-{uuid.uuid4().hex[:8]}"
    run = EvalRun(suite, (FixtureScore("alpha", 1.0, 1.0),))
    scorer, calls = _fake_scorer(run)
    handler = eval_run_handler(maker, scorer)

    before = await _spent_today(maker)
    job_id = await queue.enqueue(
        maker, queue.SYSTEM_CTX, "eval_run", {"suite": suite, "version_label": suite}
    )
    assert await worker.process_one(maker, {"eval_run": handler}) is True

    status, _attempts = await _job_status(maker, job_id)
    assert status == "failed"  # PermanentJobError → no retry
    assert calls == []  # fail-closed: the scorer never ran
    assert await EvalRunStore(maker).latest(OWNER, suite=suite, version_label=suite) is None
    assert await _spent_today(maker) == before  # not a single token charged

    await settings.upsert(queue.SYSTEM_CTX, SELF_IMPROVEMENT_KILL_SWITCH_KEY, False)


async def test_eval_run_job_refuses_fail_closed_when_budget_exhausted(
    maker: async_sessionmaker,
) -> None:
    # The other refusal term: the day's budget can't cover the estimate. Same
    # fail-closed contract — no score, no store, no spend, permanent failure.
    from datetime import UTC, datetime

    from jbrain.workflow.evalaction import EVAL_RUN_ESTIMATE_TOKENS

    settings = SqlSettingsStore(maker)
    suite = f"suite-{uuid.uuid4().hex[:8]}"
    # Budget smaller than the conservative per-run estimate → the gate refuses.
    await settings.upsert(
        queue.SYSTEM_CTX, SELF_IMPROVEMENT_BUDGET_KEY, EVAL_RUN_ESTIMATE_TOKENS - 1
    )
    # Zero out today's tally so a prior test's spend doesn't change the arithmetic.
    day = datetime.now(UTC).date().isoformat()
    await settings.upsert(queue.SYSTEM_CTX, f"{SELF_IMPROVEMENT_SPEND_PREFIX}{day}", 0)

    run = EvalRun(suite, (FixtureScore("alpha", 1.0, 1.0),))
    scorer, calls = _fake_scorer(run)
    handler = eval_run_handler(maker, scorer)

    job_id = await queue.enqueue(
        maker, queue.SYSTEM_CTX, "eval_run", {"suite": suite, "version_label": suite}
    )
    assert await worker.process_one(maker, {"eval_run": handler}) is True

    status, _attempts = await _job_status(maker, job_id)
    assert status == "failed"
    assert calls == []
    assert await EvalRunStore(maker).latest(OWNER, suite=suite, version_label=suite) is None

    await settings.upsert(
        queue.SYSTEM_CTX, SELF_IMPROVEMENT_BUDGET_KEY, EVAL_RUN_ESTIMATE_TOKENS * 100
    )


def test_registry_composes_eval_run_without_touching_action_specs() -> None:
    # The worker registers eval_run in-code (like PURGE_ACTION): it appears in the
    # composed registry's dispatch table but NOT in the always-on ACTION_SPECS (the
    # 0035 seed-lockstep set). Proven without a DB — a pure registry assertion.
    from jbrain.workflow import scheduler
    from jbrain.workflow.evalaction import EVAL_RUN_SPEC
    from jbrain.workflow.registry import ACTION_SPECS, build_registry

    assert "eval_run" not in {s.name for s in ACTION_SPECS}

    async def _noop(_payload: dict) -> None:  # pragma: no cover - never invoked
        return None

    registry = build_registry(
        (
            *ACTION_SPECS,
            scheduler.PURGE_ACTION,
            scheduler.RECONCILE_PENDING_NOTES_ACTION,
            scheduler.RECONCILE_PENDING_INTEGRATION_ACTION,
            EVAL_RUN_SPEC,
        )
    )
    impls = {
        spec.handler: _noop
        for spec in (
            *ACTION_SPECS,
            scheduler.PURGE_ACTION,
            scheduler.RECONCILE_PENDING_NOTES_ACTION,
            scheduler.RECONCILE_PENDING_INTEGRATION_ACTION,
            EVAL_RUN_SPEC,
        )
    }
    table = registry.dispatch_table(impls)  # validate() passes: every action has a handler
    assert "eval_run" in table
