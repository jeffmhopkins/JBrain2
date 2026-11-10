"""A GrowBot ticket names the robot it opens (owner, 2026-11-10: pairing is JBrain's).

docs/plans/GROWBOT_PLAN.md §2c. The PWA mints a ticket for one robot (or none); the sidecar's
redeem now answers with that code and signs it into the phone's session, so the `?id=` on the
link, which the phone can edit, no longer decides which robot the phone may join.

- `robot_code` — the robot's pairing code, or '' for a ticket that opens the creature with no
  robot. Shaped like `growbot_robots.code`.

RLS is unchanged: the table stays owner-only (`growbot_tickets_owner`).

Revision ID: 0256
Revises: 0255
Create Date: 2026-11-10
"""

from alembic import op

revision = "0256"
down_revision = "0255"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.growbot_tickets
            ADD COLUMN robot_code text NOT NULL DEFAULT ''
                CHECK (robot_code = '' OR robot_code ~ '^gb-[A-Za-z0-9]{1,32}$')
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.growbot_tickets DROP COLUMN robot_code")
