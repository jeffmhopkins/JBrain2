"""Seed the event-bound triggers + pipelines for the shadow dispatcher (W1·A2).

The three hardcoded trigger points now ALSO emit an `app.events` row
(workflow/events.py): note-created, ingest-done, resolution-changed. This
migration binds each event type to a one-action pipeline so the shadow dispatcher
(workflow/dispatcher.py) can resolve event -> trigger -> pipeline -> action and
diff its would-be enqueue against the hardcoded path (E7a):

- `note.created`     -> ingest pipeline      (action `ingest_note`)
- `note.ingested`    -> integrate pipeline   (action `integrate_note`)
- `resolution.changed` -> consolidate pipeline (action `consolidate_predicates`)

Mirrors the nightly-sweep seed shape (migration 0038): a one-action pipeline +
a trigger, here EVENT-bound (`on_event` set, `on_schedule_id` NULL) rather than
schedule-bound. The trigger `filter` pins `event_types` to the bound type (the
dispatcher also matches on it; pinning makes the binding explicit in the row) and
leaves `domains` empty — the actions are cross-domain, so the trigger accepts any
event domain and the fail-closed E2 check is the dispatcher's accept-side gate.

ADDITIVE and inert this wave: the dispatcher is SHADOW-only, so these defs drive a
diff, never a real enqueue — the hardcoded enqueues still own the path (Wave 2
cuts over). No new tables; `pipelines`/`triggers` ship in 0036. Fixed UUIDs make
the trigger ids stable for the run-log / Ops surfaces.

Revision ID: 0040
Revises: 0039
Create Date: 2026-06-15
"""

import json

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

# (event type, action, pipeline name, trigger id, description)
_EVENT_TRIGGERS = (
    (
        "note.created",
        "ingest_note",
        "event_ingest_note",
        "00000000-0000-0000-0000-0000000e0001",
        "Ingest a freshly created note (shadow of the note->ingest hardcoded trigger).",
    ),
    (
        "note.ingested",
        "integrate_note",
        "event_integrate_note",
        "00000000-0000-0000-0000-0000000e0002",
        "Integrate an indexed note (shadow of the ingest->integrate hardcoded trigger).",
    ),
    (
        "resolution.changed",
        "consolidate_predicates",
        "event_consolidate_predicates",
        "00000000-0000-0000-0000-0000000e0003",
        "Consolidate predicates after a resolution (shadow of resolution->consolidate).",
    ),
)


def _q(value: str) -> str:
    """A single-quoted SQL string literal (the seed values are trusted module
    constants; this only guards an apostrophe in a description)."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    for event_type, action, pipeline, trigger_id, description in _EVENT_TRIGGERS:
        steps = json.dumps([{"action": action, "action_version": 1, "params": {}}])
        op.execute(
            "INSERT INTO app.pipelines (name, version, steps, description)"
            f" VALUES ({_q(pipeline)}, 1, cast({_q(steps)} AS jsonb), {_q(description)})"
        )
        # Event-bound (on_event set, on_schedule_id NULL). filter pins the bound
        # event type; domains empty = accept any (the action is cross-domain).
        filter_ = json.dumps({"event_types": [event_type]})
        op.execute(
            "INSERT INTO app.triggers (id, on_event, pipeline, filter)"
            f" VALUES ({_q(trigger_id)}, {_q(event_type)}, {_q(pipeline)},"
            f" cast({_q(filter_)} AS jsonb))"
        )


def downgrade() -> None:
    for _event_type, _action, pipeline, trigger_id, _description in _EVENT_TRIGGERS:
        op.execute(f"DELETE FROM app.triggers WHERE id = '{trigger_id}'")
        op.execute(f"DELETE FROM app.pipelines WHERE name = '{pipeline}' AND version = 1")
