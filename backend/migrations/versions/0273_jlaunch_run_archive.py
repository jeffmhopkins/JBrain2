"""jlaunch runs: the owner can archive one run on its own.

A job type could already be archived (0270), taking all its finished runs with it, but
a single stale run could only be deleted. The owner asked for the Chats list's swipe
menu on runs too (2026-11-14): archive a run to tuck it away without losing its files.

The public results page does not read it (`api/jlaunch_share.py` lists the fields it
serves), and the table's RLS (0152/0153) is unchanged.

Revision ID: 0273
Revises: 0272
Create Date: 2026-11-14
"""

from alembic import op

revision = "0273"
down_revision = "0272"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.jlaunch_runs ADD COLUMN archived boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE app.jlaunch_runs DROP COLUMN archived")
