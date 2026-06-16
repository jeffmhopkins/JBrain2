"""Persisting eval runs so the promotion gate compares a candidate against a stored
baseline over time (docs/WORKFLOW_ENGINE_PLAN.md §5 Track C, E5).

The pure gate (`jbrain.workflow.promotion.promotion_decision`) takes two in-memory `EvalRun`s
and decides; today an eval run prints to stdout and is gone (`evals/run.py`). This
store gives the gate memory: it persists an `EvalRun` into `app.eval_runs` (the
per-fixture `{fixture, task, safety}` split into `scores` jsonb, plus
`suite`/`version_label`/`model`/`new_case`) and reads the latest run for a
(suite, version_label) back as an `EvalRun` — so a later candidate run can be gated
against the last stored baseline without re-running it.

Owner-only: `app.eval_runs` is owner/system audit metadata (migration 0036), so
every read/write runs on an owner-scoped session. The `scores` jsonb preserves the
two-dimensional split deliberately — a flat blob would defeat the safety-inclusive
gate (the whole point of `FixtureScore(fixture, task, safety)`).
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.workflow.promotion import EvalRun, FixtureScore


def _scores_to_json(run: EvalRun) -> str:
    """The per-fixture task/safety split as the `scores` jsonb payload — the shape
    `_run_from_scores` reconstructs an `EvalRun` from. Kept explicit (not a blob)
    so the gate's two dimensions survive the round-trip."""
    return json.dumps(
        [{"fixture": s.fixture, "task": s.task, "safety": s.safety} for s in run.scores]
    )


class MalformedEvalRunError(ValueError):
    """A stored `eval_runs.scores` value is structurally invalid. RAISED rather than
    silently dropping the bad fixture(s): dropping is fail-closed for a *candidate*
    (it can't win a fixture it lost) but fail-OPEN for a *baseline* — a dropped
    baseline fixture means a real regression on it is never compared. So a corrupt
    row must block promotion in BOTH directions, not reconstruct a partial run."""


def _run_from_scores(version: str, raw: object) -> EvalRun:
    """Rebuild an `EvalRun` from a stored `scores` jsonb value, fail-closed: a
    structurally malformed value (not a list, or any fixture missing/with a
    non-numeric — incl. bool — dimension) raises `MalformedEvalRunError` rather than
    reconstructing a partial run, so neither side can silently lose a fixture."""
    if not isinstance(raw, list):
        raise MalformedEvalRunError(f"scores is not a list: {type(raw).__name__}")
    scores: list[FixtureScore] = []
    for item in raw:
        # bool is an int subclass in Python, so exclude it explicitly — a bool is
        # not a valid 0..1 score.
        if (
            isinstance(item, dict)
            and isinstance(item.get("fixture"), str)
            and isinstance(item.get("task"), (int, float))
            and not isinstance(item.get("task"), bool)
            and isinstance(item.get("safety"), (int, float))
            and not isinstance(item.get("safety"), bool)
        ):
            scores.append(FixtureScore(item["fixture"], float(item["task"]), float(item["safety"])))
        else:
            raise MalformedEvalRunError(f"malformed fixture score: {item!r}")
    return EvalRun(version, tuple(scores))


class EvalRunStore:
    """Reads/writes `app.eval_runs` on owner-scoped sessions."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def save(
        self,
        ctx: SessionContext,
        run: EvalRun,
        *,
        suite: str,
        model: str,
        new_case: str | None = None,
    ) -> str:
        """Persist one eval run (append-only audit); return its id. `run.version`
        is the stored `version_label` the candidate/baseline lookup keys on."""
        run_id = str(uuid.uuid4())
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "INSERT INTO app.eval_runs"
                    " (id, suite, version_label, model, new_case, scores)"
                    " VALUES (:id, :suite, :label, :model, :new_case,"
                    "         cast(:scores AS jsonb))"
                ),
                {
                    "id": run_id,
                    "suite": suite,
                    "label": run.version,
                    "model": model,
                    "new_case": new_case,
                    "scores": _scores_to_json(run),
                },
            )
        return run_id

    async def latest(
        self, ctx: SessionContext, *, suite: str, version_label: str
    ) -> EvalRun | None:
        """The most recent stored run for a (suite, version_label), rebuilt into an
        `EvalRun` — the baseline a later candidate is gated against. None when the
        label has never been scored for this suite."""
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT version_label, scores::text FROM app.eval_runs"
                        " WHERE suite = :suite AND version_label = :label"
                        " ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"suite": suite, "label": version_label},
                )
            ).first()
        if row is None:
            return None
        return _run_from_scores(row.version_label, json.loads(row.scores))
