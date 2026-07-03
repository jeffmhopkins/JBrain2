"""app.encounters (+ providers/diagnoses sidecars) — the encounter projection (§4.2).

Read-models re-derived from active encounter facts (not sources of truth, #7).
`part_of_id` references app.entities (the sibling encounter ENTITY), NOT
app.encounters — no projection-to-projection FK (§4). The sidecars reference
app.encounters(entity_id) because project_emr materializes the parent encounter
row before its providers/diagnoses within the same call. `los_days` is computed
in the projector (Python) for clean NULL-handling while a stay is ongoing.

Three domain-scoped tables ⇒ each gets the RLS quartet + an isolation test
(the sidecars additionally EXISTS-join-tested: a scoped session cannot read a
provider/diagnosis whose parent encounter is out of scope).

Revision ID: 0117
Revises: 0116
Create Date: 2026-07-03
"""

from alembic import op

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None

_TABLES = ("encounters", "encounter_providers", "encounter_diagnoses")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.encounters (
            entity_id       uuid PRIMARY KEY REFERENCES app.entities(id) ON DELETE CASCADE,
            class           text,
            facility        text,
            care_unit       text,
            admitted_at     timestamptz,
            discharged_at   timestamptz,
            los_days        integer,
            disposition     text,
            part_of_id      uuid REFERENCES app.entities(id),
            source_note_id  uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
            domain_code     text NOT NULL DEFAULT 'health' REFERENCES app.domains(code),
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.encounter_providers (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            encounter_id  uuid NOT NULL REFERENCES app.encounters(entity_id) ON DELETE CASCADE,
            provider_id   uuid NOT NULL REFERENCES app.entities(id),
            provider_name text NOT NULL,
            role          text,
            domain_code   text NOT NULL DEFAULT 'health' REFERENCES app.domains(code),
            UNIQUE (encounter_id, provider_id, role)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.encounter_diagnoses (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            encounter_id  uuid NOT NULL REFERENCES app.encounters(entity_id) ON DELETE CASCADE,
            condition_id  uuid NOT NULL REFERENCES app.entities(id),
            icd10         text,
            label         text NOT NULL,
            domain_code   text NOT NULL DEFAULT 'health' REFERENCES app.domains(code),
            UNIQUE (encounter_id, condition_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX encounter_providers_encounter_idx ON app.encounter_providers (encounter_id)"
    )
    op.execute(
        "CREATE INDEX encounter_diagnoses_encounter_idx ON app.encounter_diagnoses (encounter_id)"
    )
    for table in _TABLES:
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE  ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_domain ON app.{table}
            USING (app.has_domain_scope(domain_code))
            WITH CHECK (app.has_domain_scope(domain_code))
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.encounter_diagnoses")
    op.execute("DROP TABLE app.encounter_providers")
    op.execute("DROP TABLE app.encounters")
