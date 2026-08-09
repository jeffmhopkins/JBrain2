"""Seed the nightly research-report expiry sweep (REPORT_EXPIRY_PLAN.md, W2).

Reports carry an opt-in TTL (`expires_at`, migration 0161) stamped from a preset's
`retention_days` — a daily_news briefing, say, expires seven days on. This seeds the sweep
that reaps them: the `expire_research_reports` action, hard-deleting every report past its
instant on a recurring schedule (and on demand from Ops).

Mirrors 0064 (the geofence sweep): a one-step pipeline referencing the action by name, a
schedule (`next_run_at = now()`, so it's live on the first tick after deploy), and a
`manual=true` trigger for the emergency "run now" control. The cadence is daily — expiry is
not latency-sensitive (a report that lingers a few extra hours is harmless), and re-firing is
idempotent (a row already reaped is simply gone, E4). The DELETE rides the partial
`expires_at` index, so an idle night is a cheap indexed no-op.

The `expire_research_reports` action lives in the in-code registry
(scheduler.EXPIRE_RESEARCH_REPORTS_ACTION), NOT in the app.actions seed (whose RLS test
asserts an exact set), so — like 0064 — this migration deliberately does not touch
app.actions. The action MUST ship in the same PR as this seed, else the scheduler tick fails
to resolve the pipeline every night.

Revision ID: 0162
Revises: 0161
Create Date: 2026-08-09
"""

import json

from alembic import op

revision = "0162"
down_revision = "0161"
branch_labels = None
depends_on = None

# Once a day — expiry is the slow, timing-insensitive backstop. Well above the 30s tick.
_INTERVAL_SECONDS = 86_400

_ACTION = "expire_research_reports"
_PIPELINE = "expire_research_reports"
_SCHEDULE_ID = "00000000-0000-0000-0000-000000110001"
_TRIGGER_ID = "00000000-0000-0000-0000-000000110002"
_DESCRIPTION = "Delete research-library reports whose expiry has passed."


def _q(value: str) -> str:
    """A single-quoted SQL string literal (the seed values are trusted module constants; this
    only guards an apostrophe in a description)."""
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
