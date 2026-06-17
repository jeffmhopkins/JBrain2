"""Seed the nightly `correction_mine` schedule/trigger/pipeline (Loop 3b, Tier-B).

`correction_mine` lives in the in-code registry only
(`jbrain.agent.correctionmine.CORRECTION_MINE_SPEC`, composed into the worker registry at boot like
the skill/predicate specs), so this references it by name and does NOT touch `app.actions`. One
one-action pipeline + a schedule + a `manual=true` trigger so it is Ops-fireable.

Seeded **DISABLED**: it spends self-improvement budget on LLM calls AND can propose changes to
citable truth (owner-reviewed correction notes), so the owner opts the nightly cadence in
deliberately. Staggered to 07:00 UTC (after the 06:00 predicate review). A fixed UUID makes the
trigger addressable across environments.

Revision ID: 0059
Revises: 0058
Create Date: 2026-06-17
"""

import json

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

_PIPELINE = "nightly_correction_mine"
_SCHEDULE_ID = "00000000-0000-0000-0000-0000000f000f"
_TRIGGER_ID = "00000000-0000-0000-0000-0000000f0010"
_DESCRIPTION = "Mine ended chats for owner corrections; stage owner-reviewed correction notes."
_INTERVAL = "7 hours"


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
    steps = json.dumps([{"action": "correction_mine", "action_version": 1, "params": {}}])
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
