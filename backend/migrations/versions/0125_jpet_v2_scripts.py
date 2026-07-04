"""JPet v2 — action scripts + room objects (docs/proposed/JPET_V2_PLAN.md W1–W2).

The wall pet grows from a single `action` to a short, bounded *action script* the pet
plays out, and the room gains *objects* the pet can target and carry. Both are additive
columns on the existing server-authoritative `pet_state` row (no new table — the objects
are a fixed, small set of mutable positions with no query need, so they ride as jsonb on
the row already covered by `pet_state`'s owner+domain RLS policy). The old action CHECK
enum (idle/walk/eat/play/sleep) is dropped: the v2 vocabulary (~18 primitives) is
validated in the service layer's allow-list, so it can grow without a migration.

Revision ID: 0125
Revises: 0124
Create Date: 2026-07-04
"""

from alembic import op

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The inline CHECK from 0123 was auto-named `pet_state_action_check`; the v2 vocabulary
    # is enforced in the service allow-list instead (grows without a migration).
    op.execute("ALTER TABLE app.pet_state DROP CONSTRAINT IF EXISTS pet_state_action_check")
    op.execute(
        """
        ALTER TABLE app.pet_state
            ADD COLUMN script jsonb NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN script_started_at timestamptz,
            ADD COLUMN objects jsonb NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN carrying text,
            ADD COLUMN lights_on boolean NOT NULL DEFAULT true
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.pet_state
            DROP COLUMN script,
            DROP COLUMN script_started_at,
            DROP COLUMN objects,
            DROP COLUMN carrying,
            DROP COLUMN lights_on
        """
    )
    op.execute(
        "ALTER TABLE app.pet_state ADD CONSTRAINT pet_state_action_check "
        "CHECK (action IN ('idle', 'walk', 'eat', 'play', 'sleep'))"
    )
