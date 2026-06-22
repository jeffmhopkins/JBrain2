"""Store filtered linear-acceleration magnitude on each location fix.

JBrain360 reports the phone's absolute linear-acceleration magnitude (gravity
removed, low-pass filtered to a 0.2 s time constant) alongside each GPS fix. It
rides the existing `app.location_fixes` RLS policy and grants — a new nullable
column adds no firewall surface, so no new isolation test is needed (the fix
path's RLS already gates the whole row).

Revision ID: 0080
Revises: 0079
Create Date: 2026-06-22
"""

from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.location_fixes ADD COLUMN acceleration_mps2 double precision")


def downgrade() -> None:
    op.execute("ALTER TABLE app.location_fixes DROP COLUMN acceleration_mps2")
