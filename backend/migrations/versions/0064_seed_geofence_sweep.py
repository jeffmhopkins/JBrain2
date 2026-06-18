"""Seed the recurring geofence-sweep reconciler schedule (Phase 7 Wave 3c).

The inline detection at ingest (locations/geofence.detect_transitions) is
best-effort: a detection error never breaks a stored fix, and the projector hook
that mirrors a Place's `geofence` predicate into `app.place_geofence` is likewise
best-effort. This seeds the scheduled twin — the `geofence_sweep` reconciler that
rebuilds the spatial mirror from the graph and re-evaluates each device subject's
latest fix — so a dropped projector hook or a dropped inline transition self-heals
within one interval rather than lingering until the next note touches the place.

Mirrors 0041 (the pending/integration reconcilers): a schedule (`next_run_at =
now()`, so the backstop is live on the first tick after deploy), a `manual=true`
trigger (emergency "run now" from Ops), and a one-step pipeline referencing the
action by name. The interval is 15 minutes — the sweep is the slow backstop, not
the timely path; the inline detector already handles the latency-sensitive case,
and re-firing is idempotent (a fix already reflected in geofence_state
re-evaluates to no crossing, E4).

The `geofence_sweep` action lives in the in-code registry
(scheduler.GEOFENCE_SWEEP_ACTION), NOT in the app.actions seed, so — like 0041 —
this migration deliberately does not touch app.actions. The action MUST ship in
the same PR as this seed, else the scheduler tick fails to resolve the pipeline
(DispatchResolutionError) every interval.

Revision ID: 0064
Revises: 0063
Create Date: 2026-06-18
"""

import json

from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

# Every 15 minutes — the backstop cadence (see module docstring). Well above the
# 30s scheduler tick, so a due sweep still fires promptly.
_INTERVAL_SECONDS = 900

_ACTION = "geofence_sweep"
_PIPELINE = "geofence_sweep"
_SCHEDULE_ID = "00000000-0000-0000-0000-0000000d0001"
_TRIGGER_ID = "00000000-0000-0000-0000-0000000d0002"
_DESCRIPTION = "Rebuild geofence mirrors and re-detect any missed transitions."


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
    op.execute(
        f"""
        INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at)
        VALUES ('{_SCHEDULE_ID}', {_INTERVAL_SECONDS}, 'UTC', now())
        """
    )
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
