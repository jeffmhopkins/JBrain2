"""Seed the nightly eval-run schedule, trigger, and pipeline (Phase-5 Track H·B).

The opt-in `eval_run` action (the self-improvement eval scorer, shipped H·A) becomes
a data-defined nightly schedule, mirroring the nightly sweeps (0038) and reconcilers
(0041/0042): a one-action pipeline bound by a schedule-trigger, `manual=true` so it is
emergency-fireable from Ops (`POST /ops/triggers/{id}/run`) without a restart. The
nightly run scores the live model against the curated corpus and stores an `EvalRun` —
a regression early-warning signal (a later candidate is gated against these baselines).

No new tables; no `app.actions` row. `eval_run` lives in the in-code registry only
(`EVAL_RUN_SPEC`, composed into the worker/api registry at boot, exactly like
`PURGE_ACTION` / the reconcilers), and pipeline steps resolve actions through that
registry — so this references `eval_run` by name and deliberately does NOT touch
app.actions (its RLS test asserts an exact six-row set).

Spend: each run is budget-gated (`SelfImprovementGate`, default 200k tokens/day, est.
50k/run) and kill-switchable; the schedule/trigger can be disabled from Ops. The step
carries the run params (`suite`, `version_label`) — a scheduled trigger has no per-fire
payload, so they are bound here.

Scheduling: nightly (interval 86400s). `next_run_at` is seeded to the next 03:00 UTC —
staggered an hour after the 02:00 graph sweeps; UTC is a deterministic seed and the
tick advances app-side from there. A fixed UUID makes the trigger addressable by the
Ops/run-log surfaces across environments.

Revision ID: 0044
Revises: 0043
Create Date: 2026-06-16
"""

import json

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

_ACTION = "eval_run"
_PIPELINE = "nightly_eval_run"
_SCHEDULE_ID = "00000000-0000-0000-0000-0000000c0017"
_TRIGGER_ID = "00000000-0000-0000-0000-0000000c0018"
_DESCRIPTION = "Score the note.extract eval suite against the live model (budget-gated)."

# The run params a scheduled trigger cannot supply per-fire: the whole curated suite
# under a stable `nightly` label so EvalRunStore.latest() tracks a nightly time-series.
_PARAMS = {"suite": "all", "version_label": "nightly"}

# Run hour (UTC), an hour after the 02:00 sweeps. Factored into ONE constant so the
# "already past today -> +1 day" guard and the base both pivot on the same hour (a
# split offset would fire immediately between the two hours on a fresh install).
_RUN_HOUR = 3
_NEXT_RUN_SQL = (
    f"(date_trunc('day', now() AT TIME ZONE 'UTC')"
    f" + interval '{_RUN_HOUR} hours'"
    f" + CASE WHEN (now() AT TIME ZONE 'UTC') >= date_trunc('day', now() AT TIME ZONE 'UTC')"
    f"            + interval '{_RUN_HOUR} hours'"
    f"        THEN interval '1 day' ELSE interval '0' END) AT TIME ZONE 'UTC'"
)


def _q(value: str) -> str:
    """A single-quoted SQL string literal (trusted module constants; guards an
    apostrophe in the description)."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    steps = json.dumps([{"action": _ACTION, "action_version": 1, "params": _PARAMS}])
    op.execute(
        "INSERT INTO app.pipelines (name, version, steps, description)"
        f" VALUES ({_q(_PIPELINE)}, 1, cast({_q(steps)} AS jsonb), {_q(_DESCRIPTION)})"
    )
    op.execute(
        f"""
        INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at)
        VALUES ('{_SCHEDULE_ID}', 86400, 'UTC', {_NEXT_RUN_SQL})
        """
    )
    # manual=true: the eval surfaces an emergency "run now" Ops control.
    op.execute(
        f"""
        INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)
        VALUES ('{_TRIGGER_ID}', '{_SCHEDULE_ID}', '{_PIPELINE}', true)
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM app.triggers WHERE id = '{_TRIGGER_ID}'")
    op.execute(f"DELETE FROM app.schedules WHERE id = '{_SCHEDULE_ID}'")
    op.execute(f"DELETE FROM app.pipelines WHERE name = '{_PIPELINE}' AND version = 1")
