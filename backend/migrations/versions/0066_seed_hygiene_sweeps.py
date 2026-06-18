"""Seed the nightly hygiene-sweep schedules, triggers, and pipelines (Phase-6 follow-on).

Three core-data maintenance actions (docs/HYGIENE_SWEEPS_PLAN.md), in-code only like the
other sweeps (`skill_sweep` / `consolidate_predicates`), become data-defined nightly
schedules — **disabled by default** (`enabled=false`) and emergency-fireable from Ops
(`manual=true`, `POST /ops/triggers/{id}/run`) without a restart. Mirrors 0047 (wiki sweeps).

- entity_hygiene: delete provisional orphan entities stranded by retraction/supersession.
- reembed_stale: re-embed skills/entities whose embedding_model is stale (local embed box).
- tag_consolidate: fold drift tag spellings to a canonical lower/trim/dedupe form.

No new tables; no `app.actions` row — each lives in the in-code registry (composed into the
worker at boot), so this references them by name and does NOT touch app.actions (whose RLS
test asserts an exact shipped set). A scheduled trigger has no per-fire payload (empty params).

Scheduling: nightly (86400s), staggered after the 02:00 graph sweeps; inert while disabled.
Fixed UUIDs make the triggers addressable by the Ops/run-log surfaces across environments.

Revision ID: 0066
Revises: 0065
Create Date: 2026-06-18
"""

import json

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


# (action, pipeline, schedule_id, trigger_id, run_hour, description)
_SEEDS = [
    (
        "entity_hygiene",
        "nightly_entity_hygiene",
        "00000000-0000-0000-0000-0000000c001b",
        "00000000-0000-0000-0000-0000000c001c",
        2,
        "Delete provisional orphan entities stranded by retraction/supersession.",
    ),
    (
        "reembed_stale",
        "nightly_reembed_stale",
        "00000000-0000-0000-0000-0000000c001d",
        "00000000-0000-0000-0000-0000000c001e",
        2,
        "Re-embed skills/entities whose embedding_model is stale after a model change.",
    ),
    (
        "tag_consolidate",
        "nightly_tag_consolidate",
        "00000000-0000-0000-0000-0000000c001f",
        "00000000-0000-0000-0000-0000000c0020",
        2,
        "Fold drift spellings of note tags to one canonical form.",
    ),
]


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
    for action, pipeline, sched_id, trig_id, run_hour, desc in _SEEDS:
        steps = json.dumps([{"action": action, "action_version": 1, "params": {}}])
        op.execute(
            "INSERT INTO app.pipelines (name, version, steps, description)"
            f" VALUES ({_q(pipeline)}, 1, cast({_q(steps)} AS jsonb), {_q(desc)})"
        )
        # enabled=false: ships off; the owner turns it on from Ops when they want it.
        op.execute(
            "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)"
            f" VALUES ('{sched_id}', 86400, 'UTC', {_next_run_sql(run_hour)}, false)"
        )
        op.execute(
            "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
            f" VALUES ('{trig_id}', '{sched_id}', {_q(pipeline)}, true)"
        )


def downgrade() -> None:
    for _action, pipeline, sched_id, trig_id, *_rest in _SEEDS:
        op.execute(f"DELETE FROM app.triggers WHERE id = '{trig_id}'")
        op.execute(f"DELETE FROM app.schedules WHERE id = '{sched_id}'")
        op.execute(f"DELETE FROM app.pipelines WHERE name = '{pipeline}' AND version = 1")
