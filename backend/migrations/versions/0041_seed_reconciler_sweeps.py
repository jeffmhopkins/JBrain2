"""Seed the recurring pending/integration reconciler schedules (Phase-5 Wave 2).

The two boot self-heal backfills — `reconcile_pending_notes` (ingest_state =
'pending') and `reconcile_pending_integration` (integration_state <>
'integrated') — become recurring data-defined schedules + manual triggers
(docs/WORKFLOW_ENGINE_PLAN.md §5 Wave 2). These are the dropped-event safety net
the cutover leans on: post-cutover a dropped best-effort enqueue must not strand a
note forever. The durability guarantee is the state columns; these sweeps are what
reconcile them, so promoting them off boot-only means a dropped event self-heals
within minutes — not at the next restart.

This mirrors 0038 (the nightly sweeps) exactly, with two differences:

- **Interval is 5 minutes (300s), not nightly.** A dropped event would otherwise
  leave a note unprocessed for up to a full restart cycle; 5 minutes bounds that
  staleness while staying far cheaper than the nightly LLM-backed sweeps. The
  reconcilers are cheap (one bounded INSERT…SELECT over an indexed predicate) and
  idempotent (they skip notes with an active job), so a frequent cadence is safe.
  The scheduler tick polls every 30s (scheduler.TICK_SECONDS), well below 300s, so
  a due reconciler fires promptly.
- **`next_run_at` is seeded to `now()`** (not a future 02:00), so the safety net is
  live on the very first tick after deploy rather than waiting one interval. Firing
  immediately is harmless — the reconcilers are idempotent.

Each trigger is `manual=true` so the same reconciler is emergency-fireable from Ops
(`POST /api/ops/triggers/{id}/run`) without a restart. This is ADDITIVE: the worker
boot backfills still run (belt-and-suspenders: boot + schedule). No new tables —
`pipelines`/`schedules`/`triggers` ship in 0036.

The reconciler actions are referenced by name only: they live in the in-code
registry (scheduler.RECONCILE_PENDING_*_ACTION), NOT in the app.actions seed, so
this migration deliberately does not touch app.actions (its RLS test asserts an
exact six-row set).

Revision ID: 0041
Revises: 0040
Create Date: 2026-06-15
"""

import json

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

# Every 5 minutes — the dropped-event staleness bound (see module docstring).
_INTERVAL_SECONDS = 300

# Stable ids so a trigger can be addressed by the Ops "run now" control and the
# run-log UI across environments (mirrors 0038's _SWEEPS shape).
_RECONCILERS = (
    # (action, pipeline name, schedule id, trigger id, description)
    (
        "reconcile_pending_notes",
        "reconcile_pending_notes",
        "00000000-0000-0000-0000-0000000c0011",
        "00000000-0000-0000-0000-0000000c0012",
        "Re-enqueue ingest for any note still pending — the dropped-event safety net.",
    ),
    (
        "reconcile_pending_integration",
        "reconcile_pending_integration",
        "00000000-0000-0000-0000-0000000c0013",
        "00000000-0000-0000-0000-0000000c0014",
        "Re-enqueue integration for any indexed-but-unintegrated note (bounded).",
    ),
)


def _q(value: str) -> str:
    """A single-quoted SQL string literal (the seed values are trusted module
    constants; this only guards an apostrophe in a description)."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    for action, pipeline, schedule_id, trigger_id, description in _RECONCILERS:
        steps = json.dumps([{"action": action, "action_version": 1, "params": {}}])
        op.execute(
            "INSERT INTO app.pipelines (name, version, steps, description)"
            f" VALUES ({_q(pipeline)}, 1, cast({_q(steps)} AS jsonb), {_q(description)})"
        )
        # next_run_at = now(): the safety net is live on the first tick after deploy
        # (the reconcilers are idempotent, so firing immediately is harmless).
        op.execute(
            f"""
            INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at)
            VALUES ('{schedule_id}', {_INTERVAL_SECONDS}, 'UTC', now())
            """
        )
        # manual=true: the reconciler surfaces an emergency "run now" Ops control.
        op.execute(
            f"""
            INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)
            VALUES ('{trigger_id}', '{schedule_id}', '{pipeline}', true)
            """
        )


def downgrade() -> None:
    for _action, pipeline, schedule_id, trigger_id, _description in _RECONCILERS:
        op.execute(f"DELETE FROM app.triggers WHERE id = '{trigger_id}'")
        op.execute(f"DELETE FROM app.schedules WHERE id = '{schedule_id}'")
        op.execute(f"DELETE FROM app.pipelines WHERE name = '{pipeline}' AND version = 1")
