"""Seed the entity-graph rebuild sweep's pipelines, schedules and triggers.

`graph_rebuild` (jbrain.analysis.rebuild) lives in the in-code registry only, like the
other sweeps — no `app.actions` row (that seed's RLS test asserts an exact shipped set),
so this references the action by name.

Two pipelines drive ONE action, because starting a rebuild and continuing one are
different intents:

- `graph_rebuild_start` (params `{"start": true}`) opens a run. `on_demand` schedule +
  `manual` trigger, so the owner fires it from Ops -> Automations "Run now" — the
  no-terminal path (CLAUDE.md rule 10). It never fires on a clock.
- `graph_rebuild_drain` (no params) is inert unless a run is open, and is the
  resumability backstop: a run stranded by a worker restart, or by a lost
  self-enqueue, is picked back up within the interval, and the draining phase polls
  here until re-integration settles and the chained `wiki_rebuild` can be queued. It
  ships ENABLED because an inert fire is one indexed count query, and the worker reaps
  a zero-work fire from the Ops run log (scheduler.REAPABLE_IDLE_SWEEPS).

Fixed UUIDs keep the triggers addressable by the Ops/run-log surfaces across
environments.

Revision ID: 0188
Revises: 0187
Create Date: 2026-09-08
"""

import json

from alembic import op

revision = "0188"
down_revision = "0187"
branch_labels = None
depends_on = None

_START_SCHEDULE = "00000000-0000-0000-0000-0000000c0031"
_START_TRIGGER = "00000000-0000-0000-0000-0000000c0032"
_DRAIN_SCHEDULE = "00000000-0000-0000-0000-0000000c0033"
_DRAIN_TRIGGER = "00000000-0000-0000-0000-0000000c0034"

# How often the drain schedule re-checks an open run. Short enough that a stranded
# purge resumes in minutes and a finished drain chains into the wiki rebuild promptly;
# long enough that an idle box does twelve cheap queries an hour.
_DRAIN_INTERVAL_SECONDS = 300


def _q(value: str) -> str:
    """A single-quoted SQL string literal (trusted module constants)."""
    return "'" + value.replace("'", "''") + "'"


def _pipeline(name: str, params: dict[str, object], description: str) -> None:
    steps = json.dumps([{"action": "graph_rebuild", "action_version": 1, "params": params}])
    op.execute(
        "INSERT INTO app.pipelines (name, version, steps, description)"
        f" VALUES ({_q(name)}, 1, cast({_q(steps)} AS jsonb), {_q(description)})"
    )


def upgrade() -> None:
    _pipeline(
        "graph_rebuild_start",
        {"start": True},
        "Re-derive the whole entity graph from the notes, keeping the notes.",
    )
    _pipeline(
        "graph_rebuild_drain",
        {},
        "Continue an open graph rebuild; inert when none is running.",
    )
    # on_demand: no next_run_at, so the tick never fires it; only the manual trigger does.
    op.execute(
        "INSERT INTO app.schedules (id, schedule_kind, interval_seconds, timezone,"
        " next_run_at, enabled)"
        f" VALUES ('{_START_SCHEDULE}', 'on_demand', NULL, 'UTC', NULL, true)"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
        f" VALUES ('{_START_TRIGGER}', '{_START_SCHEDULE}', 'graph_rebuild_start', true)"
    )
    op.execute(
        "INSERT INTO app.schedules (id, schedule_kind, interval_seconds, timezone,"
        " next_run_at, enabled)"
        f" VALUES ('{_DRAIN_SCHEDULE}', 'interval', {_DRAIN_INTERVAL_SECONDS}, 'UTC',"
        f" now() + make_interval(secs => {_DRAIN_INTERVAL_SECONDS}), true)"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
        f" VALUES ('{_DRAIN_TRIGGER}', '{_DRAIN_SCHEDULE}', 'graph_rebuild_drain', true)"
    )


def downgrade() -> None:
    for trigger in (_START_TRIGGER, _DRAIN_TRIGGER):
        op.execute(f"DELETE FROM app.triggers WHERE id = '{trigger}'")
    for schedule in (_START_SCHEDULE, _DRAIN_SCHEDULE):
        op.execute(f"DELETE FROM app.schedules WHERE id = '{schedule}'")
    for pipeline in ("graph_rebuild_start", "graph_rebuild_drain"):
        op.execute(f"DELETE FROM app.pipelines WHERE name = '{pipeline}' AND version = 1")
