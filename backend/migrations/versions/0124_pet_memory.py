"""JPet memory — `pet_memory`, the pet's episodic recall (docs/plans/JPET_PLAN.md W5).

Append-only notes of what happened — a child's message, a care event — so the pet
"remembers you fed it earlier": the most recent are woven into the `pet.turn` prompt
(the Generative-Agents memory loop). Owner-only + single-domain-firewalled like
`pet_state`, so a memory can never carry a fact out of the pet's safe domain.

Revision ID: 0124
Revises: 0123
Create Date: 2026-07-04
"""

from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.pet_memory (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            domain_code text NOT NULL REFERENCES app.domains(code),
            kind text NOT NULL DEFAULT 'said'
                CHECK (kind IN ('said', 'care', 'thought')),
            body text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX pet_memory_recent_idx ON app.pet_memory (domain_code, created_at DESC)"
    )
    op.execute("ALTER TABLE app.pet_memory ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.pet_memory FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY pet_memory_owner ON app.pet_memory
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.pet_memory TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.pet_memory")
