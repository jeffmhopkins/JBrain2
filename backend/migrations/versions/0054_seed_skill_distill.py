"""Seed the nightly `skill_distill` schedule/trigger/pipeline (Loop 2, Wave 2).

`skill_distill` lives in the in-code registry only (`jbrain.agent.skilldistill.SKILL_DISTILL_SPEC`,
composed into the worker registry at boot like `EVAL_RUN_SPEC` / the wiki specs), so this migration
references it by name and does NOT touch `app.actions` (its RLS test asserts an exact set). One
one-action pipeline + a schedule + a `manual=true` trigger so it is Ops-fireable.

Seeded **DISABLED**: distillation only writes *shadow* skills + stages owner proposals (nothing
goes live without owner review), but it spends self-improvement budget on LLM calls, so the owner
opts it in deliberately. Staggered to 04:00 UTC (after the 03:00 eval). `next_run_at` rolls to
tomorrow if 04:00 has passed; a fixed UUID makes the trigger addressable across environments.

Revision ID: 0054
Revises: 0053
Create Date: 2026-06-17
"""

import json

from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

_PIPELINE = "nightly_skill_distill"
_SCHEDULE_ID = "00000000-0000-0000-0000-0000000f0009"
_TRIGGER_ID = "00000000-0000-0000-0000-0000000f000a"
_DESCRIPTION = "Distill skills from successful agent runs into owner-reviewed shadow skills."
_INTERVAL = "4 hours"


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
    steps = json.dumps([{"action": "skill_distill", "action_version": 1, "params": {}}])
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
