"""Seed the recurring unembedded-notes reconciler schedule (Phase-5 completion, Track S).

The third and last boot-only self-heal backfill — `reconcile_unembedded_notes`
(`queue.backfill_unembedded_notes`) — becomes a recurring data-defined schedule +
manual trigger, exactly as 0041 promoted the pending-ingest / pending-integration
reconcilers. A dropped `embed_note` enqueue otherwise strands a note's chunks
unembedded until the next worker restart; promoting this sweep off boot-only means
it self-heals within minutes instead.

This is a near-mechanical mirror of 0041, with the same two deviations from the
nightly sweeps (0038):

- **Interval is 5 minutes (300s), not nightly.** The reconciler is cheap (one
  bounded INSERT…SELECT over an indexed predicate) and idempotent (it skips notes
  with an active `embed_note` job), so a frequent cadence is safe and bounds the
  dropped-event staleness window. The scheduler tick polls every 30s
  (scheduler.TICK_SECONDS), well below 300s, so a due reconciler fires promptly.
- **`next_run_at` is seeded to `now()`**, so the safety net is live on the very
  first tick after deploy rather than after one interval. Firing immediately is
  harmless — the reconciler is idempotent.

The trigger is `manual=true` so the reconciler is emergency-fireable from Ops
(`POST /api/ops/triggers/{id}/run`) without a restart. This is ADDITIVE: the worker
boot backfill still runs (belt-and-suspenders: boot + schedule). No new tables —
`pipelines`/`schedules`/`triggers` ship in 0036.

The reconciler action is referenced by name only: it lives in the in-code registry
(scheduler.RECONCILE_UNEMBEDDED_NOTES_ACTION), NOT in the app.actions seed, so this
migration deliberately does not touch app.actions (its RLS test asserts an exact
six-row set).

Revision ID: 0042
Revises: 0041
Create Date: 2026-06-16
"""

import json

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

# Every 5 minutes — the dropped-event staleness bound (see module docstring,
# mirrors 0041's _INTERVAL_SECONDS).
_INTERVAL_SECONDS = 300

# A fresh stable schedule/trigger UUID pair continuing 0041's `…000c00xx` series
# (0041 used 0011–0014; this continues at 0015/0016) so the trigger is addressable
# by the Ops "run now" control across environments.
_ACTION = "reconcile_unembedded_notes"
_PIPELINE = "reconcile_unembedded_notes"
_SCHEDULE_ID = "00000000-0000-0000-0000-0000000c0015"
_TRIGGER_ID = "00000000-0000-0000-0000-0000000c0016"
_DESCRIPTION = "Re-enqueue embed for any note with unembedded chunks (dropped-event safety net)."


def _q(value: str) -> str:
    """A single-quoted SQL string literal (the seed values are trusted module
    constants; this only guards an apostrophe in a description)."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    steps = json.dumps([{"action": _ACTION, "action_version": 1, "params": {}}])
    op.execute(
        "INSERT INTO app.pipelines (name, version, steps, description)"
        f" VALUES ({_q(_PIPELINE)}, 1, cast({_q(steps)} AS jsonb), {_q(_DESCRIPTION)})"
    )
    # next_run_at = now(): the safety net is live on the first tick after deploy
    # (the reconciler is idempotent, so firing immediately is harmless).
    op.execute(
        f"""
        INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at)
        VALUES ('{_SCHEDULE_ID}', {_INTERVAL_SECONDS}, 'UTC', now())
        """
    )
    # manual=true: the reconciler surfaces an emergency "run now" Ops control.
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
