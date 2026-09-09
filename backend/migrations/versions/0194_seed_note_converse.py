"""Seed the `note_converse` pipeline + trigger, bound to `note.ingested`.

W2/T4 of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md: an ingested note now ALSO opens
an agent conversation about itself (`analysis/converse.py`), and this is the binding
that makes it happen. `note_converse` lives in the in-code registry only, like the other
post-Phase-4 actions — no `app.actions` row (that seed's RLS test asserts an exact
shipped set), so the pipeline references the action by name.

BESIDE, not instead of. `note.ingested` already drives `event_integrate_note` (0040) and
the two EMR triggers, and it keeps driving all of them. D13 is explicit that no PR
removes a producer before its replacement is merged, so this wave runs both paths: the
shipped pipeline still extracts the facts and writes the graph, and the conversation
reads the note in a thread the owner can open.

ENABLED, and the cost is real and worth stating plainly — per EVENT, which is not per
note. `note.ingested` fires on every settled ingest, so a note re-ingested is a note
charged again, in a second thread: an attachment landing on an already-ingested note (a
photo captured WITH its `attachments_expected` hint pays once, because the emit gate
defers until OCR is done; one added later, or a client that sends no hint, does not), and
every D6 clarification, since `append_clarification` enqueues `ingest_note` itself. A
corpus rebuild costs nothing here: `backfill_pending_integration` enqueues
`integrate_note` straight into `app.jobs` and emits no event.

Each of those turns produces NO graph writes in W2 — the `note_ingest` persona's tool
allowlist is an empty frozenset (D16, 0192), so there is nothing it can write. What it
produces is the thread, which is the entire point of the wave landing: W2's honest retreat
point is "the agent reads a note in a visible thread", and a trigger seeded disabled would
ship that retreat point already retreated from. This is plan risk 4 ("Cost. 5-10x
inference per note"), accepted at ratification. Turning it off needs no terminal: it is a
row in Ops -> Automations (CLAUDE.md #10).

KNOWN LIMIT, on old code against this schema. `resolve_event` returns a whole-event error
on the first trigger it cannot resolve and discards the enqueues it had already computed,
so a worker running a PRE-0194 image against a migrated database resolves this trigger to
an action its in-code registry does not have and drops that event's `integrate_note` with
it — the event is then marked dispatched. Every note stops being integrated for as long as
that pairing stands, which is the one window where D13's "no producer removed before its
replacement is merged" is transiently violated. The normal `Ops -> Update` path does not
reach it (the worker is quiesced across `migrate`); it needs an image rolled back without
its schema. The integration recovers on its own through the recurring
`backfill_pending_integration`; the conversation does not, and in this wave writes nothing.
The fix belongs to the dispatcher — separating the E3 resolution error, which should be
per-trigger, from the E1/E2 authorization errors, which must stay whole-event fail-closed.

The trigger's `filter` pins `event_types` to the bound type, exactly as 0040 does, and
leaves `domains` empty — the action is cross-domain, so it accepts any note's domain and
the fail-closed E2 check on the dispatcher's accept side is the gate. `forward_keys`
stays at the default, so the job payload is `{note_id}`; the EMR markers the same event
carries are inert here.

One conversation per note is NOT this migration's job. `note_conversations_one_live`
(0191) is the authority, and `dispatcher._already_active` is the graceful arm in front of
it so a re-delivered event is a logged skip rather than an IntegrityError in a worker.

A fixed UUID keeps the trigger addressable by the run-log / Ops surfaces across
environments, and the downgrade removes both rows.

Revision ID: 0194
Revises: 0193
Create Date: 2026-09-09
"""

import json

from alembic import op

revision = "0194"
down_revision = "0193"
branch_labels = None
depends_on = None

_EVENT = "note.ingested"
_ACTION = "note_converse"
_PIPELINE = "event_note_converse"
_TRIGGER = "00000000-0000-0000-0000-0000000e0004"
_DESCRIPTION = "Open the note's agent conversation and read the note in it."


def _q(value: str) -> str:
    """A single-quoted SQL string literal (trusted module constants)."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    steps = json.dumps([{"action": _ACTION, "action_version": 1, "params": {}}])
    op.execute(
        "INSERT INTO app.pipelines (name, version, steps, description)"
        f" VALUES ({_q(_PIPELINE)}, 1, cast({_q(steps)} AS jsonb), {_q(_DESCRIPTION)})"
    )
    filter_ = json.dumps({"event_types": [_EVENT]})
    op.execute(
        "INSERT INTO app.triggers (id, on_event, pipeline, filter)"
        f" VALUES ({_q(_TRIGGER)}, {_q(_EVENT)}, {_q(_PIPELINE)},"
        f" cast({_q(filter_)} AS jsonb))"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM app.triggers WHERE id = '{_TRIGGER}'")
    op.execute(f"DELETE FROM app.pipelines WHERE name = '{_PIPELINE}' AND version = 1")
