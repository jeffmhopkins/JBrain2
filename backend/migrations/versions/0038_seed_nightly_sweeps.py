"""Seed the nightly-sweep schedules, triggers, and pipelines (Phase-5 Track B).

The periodic work that is boot-only self-heal today — `consolidate_predicates`,
`sync_predicates`, and the deleted-note-artifact purge — becomes data-defined
nightly schedules (docs/archive/WORKFLOW_ENGINE_PLAN.md §5 Track B). Each is a one-action
pipeline bound by a schedule-trigger; the trigger is `manual=true` so the same
sweep is emergency-fireable from Ops (`POST /ops/triggers/{id}/run`) without a
restart (E4). This is ADDITIVE: the worker boot backfills still run, so behavior
is unchanged on first boot; the schedules add a recurring + on-demand path on top.

No new tables — `pipelines`/`schedules`/`triggers` ship in 0036. The purge action
(`purge_deleted_artifacts`) is referenced by name only: it lives in the in-code
registry (scheduler.PURGE_ACTION), NOT in the app.actions seed, so this migration
deliberately does not touch app.actions (its RLS test asserts an exact six-row set).

Scheduling: nightly (interval 86400s). `next_run_at` is seeded to the next 02:00
UTC; the owner-local 02:00 refinement reads `owner_timezone` at runtime and is a
later concern — UTC is a safe, deterministic seed and the tick advances app-side
from there. Fixed UUIDs make the seed idempotent-ish to reason about and let the
Ops/run-log surfaces reference a stable trigger id.

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-15
"""

import json

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

# Stable ids so a trigger can be addressed by the Ops "run now" control and the
# run-log UI across environments.
_SWEEPS = (
    # (action, pipeline name, schedule id, trigger id, description)
    (
        "consolidate_predicates",
        "nightly_consolidate_predicates",
        "00000000-0000-0000-0000-0000000c0001",
        "00000000-0000-0000-0000-0000000c0002",
        "Normalize predicate drift left by older prompt versions.",
    ),
    (
        "sync_predicates",
        "nightly_sync_predicates",
        "00000000-0000-0000-0000-0000000c0003",
        "00000000-0000-0000-0000-0000000c0004",
        "Keep the canonical_predicates index in step with the schema registry.",
    ),
    (
        "purge_deleted_artifacts",
        "nightly_purge_deleted_artifacts",
        "00000000-0000-0000-0000-0000000c0005",
        "00000000-0000-0000-0000-0000000c0006",
        "Purge artifacts of notes deleted before the cascade existed.",
    ),
)

# 02:00 UTC nightly; the next occurrence is computed at apply time so a fresh
# install does not fire immediately on the first tick.
_NEXT_RUN_SQL = (
    "(date_trunc('day', now() AT TIME ZONE 'UTC')"
    " + interval '2 hours'"
    " + CASE WHEN (now() AT TIME ZONE 'UTC') >= date_trunc('day', now() AT TIME ZONE 'UTC')"
    "            + interval '2 hours'"
    "        THEN interval '1 day' ELSE interval '0' END) AT TIME ZONE 'UTC'"
)


def _q(value: str) -> str:
    """A single-quoted SQL string literal (the seed values are trusted module
    constants; this only guards an apostrophe in a description)."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    for action, pipeline, schedule_id, trigger_id, description in _SWEEPS:
        steps = json.dumps([{"action": action, "action_version": 1, "params": {}}])
        op.execute(
            "INSERT INTO app.pipelines (name, version, steps, description)"
            f" VALUES ({_q(pipeline)}, 1, cast({_q(steps)} AS jsonb), {_q(description)})"
        )
        op.execute(
            f"""
            INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at)
            VALUES ('{schedule_id}', 86400, 'UTC', {_NEXT_RUN_SQL})
            """
        )
        # manual=true: the sweep surfaces an emergency "run now" Ops control.
        op.execute(
            f"""
            INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)
            VALUES ('{trigger_id}', '{schedule_id}', '{pipeline}', true)
            """
        )


def downgrade() -> None:
    for _action, pipeline, schedule_id, trigger_id, _description in _SWEEPS:
        op.execute(f"DELETE FROM app.triggers WHERE id = '{trigger_id}'")
        op.execute(f"DELETE FROM app.schedules WHERE id = '{schedule_id}'")
        op.execute(f"DELETE FROM app.pipelines WHERE name = '{pipeline}' AND version = 1")
