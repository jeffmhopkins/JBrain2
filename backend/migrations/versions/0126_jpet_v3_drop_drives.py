"""JPet v3 — drop the drive meters (docs/plans/JPET_V3_PLAN.md W1).

A needs-free "just alive and playful" pet is the design (validated — PF.Magic's Petz
exposed no meters and made mood readable from behaviour), and the pet's continuous
autonomous life now runs on the wall, not a server tick. So the four drive columns
(food/energy/fun/love) are removed from `pet_state`; the derived `mood`/`emotion` labels
stay as plain strings for the phone header. RLS/ownership are unchanged (same table).

Revision ID: 0126
Revises: 0125
Create Date: 2026-07-04
"""

from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.pet_state "
        "DROP COLUMN food, DROP COLUMN energy, DROP COLUMN fun, DROP COLUMN love"
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.pet_state
            ADD COLUMN food double precision NOT NULL DEFAULT 80,
            ADD COLUMN energy double precision NOT NULL DEFAULT 80,
            ADD COLUMN fun double precision NOT NULL DEFAULT 70,
            ADD COLUMN love double precision NOT NULL DEFAULT 70
        """
    )
