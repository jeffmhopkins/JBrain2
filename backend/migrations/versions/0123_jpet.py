"""JPet — `pet_state`, the server-authoritative wall-pet row (docs/plans/JPET_PLAN.md W0).

The pet's truth lives here: one row per pet, holding its drives (food/energy/fun/
love as 0–100 satisfaction), derived mood, sleep flag, floor position/target the
Wall renders, and the current utterance/emotion. Both surfaces (the 3D Wall and the
phone Control screen) are views of this row; a drives tick and `/pet/command` mutate
it and the change fans out over SSE.

Owner-only and single-domain-firewalled, exactly like `lists` (CLAUDE.md rule 3): a
pet lives in one in-scope domain (the safe family domain, `general`, by default), a
non-owner principal sees none, and a narrowed session cannot read a pet in a domain
it lacks — so the kids' pet can never surface a health/finance/location fact because
its row isn't even visible out of scope. One pet per (principal, domain): UNIQUE.

Revision ID: 0123
Revises: 0122
Create Date: 2026-07-04
"""

from alembic import op

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.pet_state (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            domain_code text NOT NULL REFERENCES app.domains(code),
            name text NOT NULL,
            food double precision NOT NULL DEFAULT 80,
            energy double precision NOT NULL DEFAULT 80,
            fun double precision NOT NULL DEFAULT 70,
            love double precision NOT NULL DEFAULT 70,
            mood text NOT NULL DEFAULT 'neutral',
            emotion text NOT NULL DEFAULT 'neutral',
            speech text,
            asleep boolean NOT NULL DEFAULT false,
            pos_x double precision NOT NULL DEFAULT 0,
            pos_z double precision NOT NULL DEFAULT 0,
            target_x double precision NOT NULL DEFAULT 0,
            target_z double precision NOT NULL DEFAULT 0,
            facing double precision NOT NULL DEFAULT 0,
            action text NOT NULL DEFAULT 'idle'
                CHECK (action IN ('idle', 'walk', 'eat', 'play', 'sleep')),
            last_tick_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (principal_id, domain_code)
        )
        """
    )
    op.execute("ALTER TABLE app.pet_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.pet_state FORCE ROW LEVEL SECURITY")
    # Owner-only AND domain-narrowed: the pet lives in exactly one in-scope domain,
    # and a non-owner principal sees/creates none — the kids-safety firewall.
    op.execute(
        """
        CREATE POLICY pet_state_owner ON app.pet_state
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.pet_state TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.pet_state")
