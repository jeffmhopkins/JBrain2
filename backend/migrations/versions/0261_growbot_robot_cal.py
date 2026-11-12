"""A GrowBot robot carries its own leg calibration (the Body tab's Calibrate section).

Upstream keeps the calibration (`gb_walk_cal`: the by-eye leg trims, the left/right swap, the
leg and IMU signs, the walk gain) in the localStorage of whichever phone set it, so a second
phone, a reset browser or a new persona drives the same legs crooked. It belongs to the body,
so it lives on the robot's row and the creature page copies it in (growbot/README.md).

- `cal` — the calibration as the creature reads it, or NULL for a robot nobody has
  calibrated from the PWA (a phone then keeps whatever it already holds).

RLS is unchanged: the table stays owner-only (`growbot_robots_owner`).

Revision ID: 0261
Revises: 0260
Create Date: 2026-11-12
"""

from alembic import op

revision = "0261"
down_revision = "0260"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.growbot_robots
            ADD COLUMN cal jsonb CHECK (cal IS NULL OR jsonb_typeof(cal) = 'object')
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.growbot_robots DROP COLUMN cal")
