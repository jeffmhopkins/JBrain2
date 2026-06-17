"""Seed the nightly `predicate_review` schedule/trigger/pipeline (Loop 3a, Wave 2).

`predicate_review` lives in the in-code registry only
(`jbrain.agent.predicatereview.PREDICATE_REVIEW_SPEC`, composed into the worker registry at boot
like the skill specs), so this references it by name and does NOT touch `app.actions`. One
one-action pipeline + a schedule + a `manual=true` trigger so it is Ops-fireable.

Seeded **DISABLED**: the action only STAGES owner proposals (nothing is applied without owner
review), but the owner opts the nightly cadence in deliberately. Staggered to 06:00 UTC (after the
05:00 skill sweep). A fixed UUID makes the trigger addressable across environments.

Revision ID: 0058
Revises: 0057
Create Date: 2026-06-17
"""

import json

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

_PIPELINE = "nightly_predicate_review"
_SCHEDULE_ID = "00000000-0000-0000-0000-0000000f000d"
_TRIGGER_ID = "00000000-0000-0000-0000-0000000f000e"
_DESCRIPTION = "Propose owner-reviewed resolutions for open new_predicate cards."
_INTERVAL = "6 hours"


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _next_run_sql(interval: str) -> str:
    return (
        f"(date_trunc('day', now() AT TIME ZONE 'UTC') + interval '{interval}'"
        f" + CASE WHEN (now() AT TIME ZONE 'UTC') >= date_trunc('day', now() AT TIME ZONE 'UTC')"
        f"            + interval '{interval}'"
        f"        THEN interval '1 day' ELSE interval '0' END) AT TIME ZONE 'UTC'"
    )


def upgrade() -> None:
    steps = json.dumps([{"action": "predicate_review", "action_version": 1, "params": {}}])
    op.execute(
        "INSERT INTO app.pipelines (name, version, steps, description)"
        f" VALUES ({_q(_PIPELINE)}, 1, cast({_q(steps)} AS jsonb), {_q(_DESCRIPTION)})"
    )
    op.execute(
        "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)"
        f" VALUES ('{_SCHEDULE_ID}', 86400, 'UTC', {_next_run_sql(_INTERVAL)}, false)"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
        f" VALUES ('{_TRIGGER_ID}', '{_SCHEDULE_ID}', {_q(_PIPELINE)}, true)"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM app.triggers WHERE id = '{_TRIGGER_ID}'")
    op.execute(f"DELETE FROM app.schedules WHERE id = '{_SCHEDULE_ID}'")
    op.execute(f"DELETE FROM app.pipelines WHERE name = '{_PIPELINE}' AND version = 1")
