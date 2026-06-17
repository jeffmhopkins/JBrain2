"""Seed the nightly `skill_sweep` schedule/trigger/pipeline (Loop 2, Wave 3).

`skill_sweep` lives in the in-code registry only (`jbrain.agent.skillsweep.SKILL_SWEEP_SPEC`,
composed into the worker registry at boot like `SKILL_DISTILL_SPEC`), so this migration references
it by name and does NOT touch `app.actions` (its RLS test asserts an exact set). One one-action
pipeline + a schedule + a `manual=true` trigger so it is Ops-fireable.

Seeded **DISABLED**: the sweep is reversible hygiene (it only demotes the least-useful actives back
to shadow; the owner re-promotes), but it changes which skills retrieval can surface, so the owner
opts it in deliberately. Staggered to 05:00 UTC (after the 04:00 distill). `next_run_at` rolls to
tomorrow if 05:00 has passed; a fixed UUID makes the trigger addressable across environments.

Revision ID: 0055
Revises: 0054
Create Date: 2026-06-17
"""

import json

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

_PIPELINE = "nightly_skill_sweep"
_SCHEDULE_ID = "00000000-0000-0000-0000-0000000f000b"
_TRIGGER_ID = "00000000-0000-0000-0000-0000000f000c"
_DESCRIPTION = "Cap active skills per domain, demoting the least-useful to shadow (reversible)."
_INTERVAL = "5 hours"


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
    steps = json.dumps([{"action": "skill_sweep", "action_version": 1, "params": {}}])
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
