"""Seed the inbox-triage schedule, trigger, and pipeline (archivist).

The `triage_inbox` action (docs/EMAIL_ARCHIVIST_PLAN.md) becomes a data-defined daily
schedule — **disabled by default** (`enabled=false`) and emergency-fireable from Ops
(`manual=true`, `POST /ops/triggers/{id}/run`) without a restart. Mirrors 0066 (hygiene
sweeps) / 0047 (wiki sweeps).

`triage_inbox` classifies the newest day of inbox mail into `triaged/*` priority labels
and archives it; one run triages a single day, so repeated runs walk back through the
backlog. The owner runs it manually for now; the disabled schedule is the on-switch.

In-code only like the other sweeps — no `app.actions` row (whose RLS test asserts the
exact shipped set), so this references the action by name and does NOT touch app.actions.
A scheduled trigger has no per-fire payload (empty params). Fixed UUIDs make the trigger
addressable by the Ops/run-log surfaces across environments.

Revision ID: 0096
Revises: 0095
Create Date: 2026-06-25
"""

import json

from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None

_ACTION = "triage_inbox"
_PIPELINE = "daily_inbox_triage"
_SCHEDULE_ID = "00000000-0000-0000-0000-000000100001"
_TRIGGER_ID = "00000000-0000-0000-0000-000000100002"
_RUN_HOUR = 3  # staggered after the 02:00 graph/hygiene sweeps; inert while disabled
_DESC = "Classify the newest day of inbox mail into triaged/* labels and archive it."


def _q(value: str) -> str:
    """A single-quoted SQL string literal (trusted module constants)."""
    return "'" + value.replace("'", "''") + "'"


def _next_run_sql(run_hour: int) -> str:
    return (
        f"(date_trunc('day', now() AT TIME ZONE 'UTC')"
        f" + interval '{run_hour} hours'"
        f" + CASE WHEN (now() AT TIME ZONE 'UTC') >= date_trunc('day', now() AT TIME ZONE 'UTC')"
        f"            + interval '{run_hour} hours'"
        f"        THEN interval '1 day' ELSE interval '0' END) AT TIME ZONE 'UTC'"
    )


def upgrade() -> None:
    steps = json.dumps([{"action": _ACTION, "action_version": 1, "params": {}}])
    op.execute(
        "INSERT INTO app.pipelines (name, version, steps, description)"
        f" VALUES ({_q(_PIPELINE)}, 1, cast({_q(steps)} AS jsonb), {_q(_DESC)})"
    )
    # enabled=false: ships off; the owner turns it on from Ops when they want it.
    op.execute(
        "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)"
        f" VALUES ('{_SCHEDULE_ID}', 86400, 'UTC', {_next_run_sql(_RUN_HOUR)}, false)"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
        f" VALUES ('{_TRIGGER_ID}', '{_SCHEDULE_ID}', {_q(_PIPELINE)}, true)"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM app.triggers WHERE id = '{_TRIGGER_ID}'")
    op.execute(f"DELETE FROM app.schedules WHERE id = '{_SCHEDULE_ID}'")
    op.execute(f"DELETE FROM app.pipelines WHERE name = '{_PIPELINE}' AND version = 1")
