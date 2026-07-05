"""JPet v3 W3 — the pet's colour (docs/plans/JPET_V3_PLAN.md W3).

The kids can recolour the robot on command ("turn red", a phone palette). The chosen
colour is durable state on `pet_state` (a plain name the wall maps to a neon RGB, or
`rainbow` for a cycle); null means the default synthwave palette. RLS unchanged.

Revision ID: 0127
Revises: 0126
Create Date: 2026-07-05
"""

from alembic import op

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.pet_state ADD COLUMN color text")


def downgrade() -> None:
    op.execute("ALTER TABLE app.pet_state DROP COLUMN color")
