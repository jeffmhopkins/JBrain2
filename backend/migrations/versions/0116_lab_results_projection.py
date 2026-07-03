"""app.lab_results — the per-reading lab projection (EMR import §4.1).

A read-model re-derived from active `value` facts (not a source of truth, #7),
built like app.appointments: one row per `value` FACT (a corrected draw has two —
a superseded predecessor + its active head, so `source_fact_id` is in the unique
key), with a partial unique index imposing exactly one CURRENT reading per draw.
`report_status` is a DERIVED column (from the value fact's lifecycle + supersession
chain, §3.5) — never a stored status fact. `encounter_id` references app.entities
(the encounter ENTITY), NOT app.encounters — a projection never FKs a sibling
projection row (the ordering-hazard fix, §4). `specimen_id NOT NULL DEFAULT ''`
closes the NULL-distinctness hole so OCR-unreadable draws collide purely on
collected_at, identical to the fact qualifier.

Domain-scoped table ⇒ the RLS quartet (ENABLE+FORCE, has_domain_scope USING/WITH
CHECK, grants incl. DELETE since projections re-derive) + an isolation test.

Revision ID: 0116
Revises: 0115
Create Date: 2026-07-03
"""

from alembic import op

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.lab_results (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            entity_id        uuid NOT NULL REFERENCES app.entities(id) ON DELETE CASCADE,
            analyte          text NOT NULL,
            loinc            text,
            value_num        double precision,
            value_text       text,
            unit             text,
            ref_low          double precision,
            ref_high         double precision,
            ref_text         text,
            interpretation   text CHECK (interpretation IN
                ('normal','high','low','abnormal','critical','borderline','indeterminate')),
            collected_at     timestamptz NOT NULL,
            specimen_id      text NOT NULL DEFAULT '',
            performing_lab   text,
            orderer          text,
            encounter_id     uuid REFERENCES app.entities(id),
            report_status    text NOT NULL DEFAULT 'final'
                CHECK (report_status IN
                    ('registered','preliminary','final','amended','corrected',
                     'cancelled','entered-in-error')),
            is_current       boolean NOT NULL DEFAULT true,
            superseded_by_id uuid REFERENCES app.lab_results(id),
            source_note_id   uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
            source_fact_id   uuid NOT NULL REFERENCES app.facts(id),
            domain_code      text NOT NULL DEFAULT 'health' REFERENCES app.domains(code),
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz,
            UNIQUE (entity_id, collected_at, specimen_id, source_fact_id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX lab_results_current_draw_idx"
        " ON app.lab_results (entity_id, collected_at, specimen_id) WHERE is_current"
    )
    op.execute(
        "CREATE INDEX lab_results_series_idx ON app.lab_results (entity_id, collected_at DESC)"
    )
    op.execute(
        "CREATE INDEX lab_results_abnormal_idx ON app.lab_results (collected_at DESC)"
        " WHERE interpretation IN ('critical','high','low','abnormal')"
    )
    op.execute("ALTER TABLE app.lab_results ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.lab_results FORCE  ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY lab_results_domain ON app.lab_results
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.lab_results TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.lab_results")
