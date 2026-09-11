"""Retire the `integrate_note` producer: its action row, its event trigger, and the one
table only it ever wrote.

R4 of docs/plans/AGENT_INGEST_REWRITE.md deletes the model-side ingestion chain —
note.extract -> Integrator -> arbiter -> apply_intent. The note conversation
(`note_converse`, seeded in 0194) has been writing the same graph beside it since R1 and
is now the note's only producer.

**This migration is not optional bookkeeping; it must land with the image.**
`dispatcher.resolve_event` fails a WHOLE event when one of its triggers names an action
the in-code registry does not have, and `note.ingested` drives both `event_integrate_note`
(0040) and `event_note_converse` (0194). A worker on the new image against an unmigrated
database would therefore drop the conversation's enqueue too, and mark the event
dispatched — the failure mode 0194's own docstring names from the other direction. The
`Ops -> Update` path quiesces the worker across `migrate`, so the normal update is safe
(CLAUDE.md #10).

`app.resolution_pin` goes with the chain rather than just its rows: it memoized
Integrator decisions (`analysis/pins.py`, `analysis/persist.py`, both deleted) and had no
other writer and no reader outside them. `app.graph_rebuild_runs` and `app.eval_runs`
stay — neither is producer-specific.

What is deliberately NOT touched: `app.jobs` rows of kind `integrate_note` (history, and
a queued one is already unreachable because the handler is gone — the worker logs an
unknown kind and fails the job, which is the visible outcome rather than a silent one),
`app.runs` rows of kind `integration` (audit), and every `review_items` row the analyzer
filed. Those cards keep their renderers and their resolution arms: R4 removed no card
kind, so an open card the analyzer left behind still opens, still renders, and is still
the owner's to accept, reject or dismiss. Nothing sweeps them automatically any more —
both settle halves are scoped to their filer (0197) and their filer is gone — so they
retire by the owner's own hand, by the fact being retracted (`purge.delete_review_items`),
by note deletion, or by the corpus rebuild.

Revision ID: 0200
Revises: 0199
Create Date: 2026-09-11
"""

from alembic import op

revision = "0200"
down_revision = "0199"
branch_labels = None
depends_on = None

_TRIGGER_ID = "00000000-0000-0000-0000-0000000e0002"
_PIPELINE = "event_integrate_note"


def upgrade() -> None:
    # Trigger first: it references the pipeline by name+version.
    op.execute(f"DELETE FROM app.triggers WHERE id = '{_TRIGGER_ID}'")
    op.execute(f"DELETE FROM app.pipelines WHERE name = '{_PIPELINE}'")
    op.execute("DELETE FROM app.actions WHERE name = 'integrate_note'")
    op.execute("DROP TABLE IF EXISTS app.resolution_pin")


def downgrade() -> None:
    # Reversible in SHAPE, not in content: the pins were a memo of decisions a deleted
    # agent made, so there is nothing to restore into the recreated table. The action,
    # pipeline and trigger rows come back exactly as 0035/0040 seeded them.
    op.execute(
        """
        CREATE TABLE app.resolution_pin (
            note_id uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
            chunk_id uuid NOT NULL REFERENCES app.chunks(id) ON DELETE CASCADE,
            occurrence_index integer NOT NULL,
            decision_kind text NOT NULL
                CHECK (decision_kind IN ('identity', 'predicate_key')),
            surface text NOT NULL,
            span_text_hash text NOT NULL,
            entity_id uuid REFERENCES app.entities(id) ON DELETE CASCADE,
            normalized_predicate text,
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (note_id, chunk_id, occurrence_index, decision_kind),
            CHECK (
                (decision_kind = 'identity'
                    AND entity_id IS NOT NULL AND normalized_predicate IS NULL)
                OR (decision_kind = 'predicate_key'
                    AND normalized_predicate IS NOT NULL AND entity_id IS NULL)
            )
        )
        """
    )
    op.execute("ALTER TABLE app.resolution_pin ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.resolution_pin FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY resolution_pin_domain ON app.resolution_pin
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.resolution_pin TO jbrain_app")
    op.execute(
        "INSERT INTO app.actions (name, version, handler, mutating, cost_class, dedup_key_expr)"
        " VALUES ('integrate_note', 1, 'integrate_note', true, 'expensive', 'note_id')"
    )
    op.execute(
        "INSERT INTO app.pipelines (name, version, steps, description) VALUES ("
        f"'{_PIPELINE}', 1,"
        """ cast('[{"action": "integrate_note", "action_version": 1, "params": {}}]' AS jsonb),"""
        " 'Integrate an indexed note (shadow of the ingest->integrate hardcoded trigger).')"
    )
    op.execute(
        "INSERT INTO app.triggers (id, on_event, pipeline, filter) VALUES ("
        f"'{_TRIGGER_ID}', 'note.ingested', '{_PIPELINE}',"
        """ cast('{"event_types": ["note.ingested"]}' AS jsonb))"""
    )
