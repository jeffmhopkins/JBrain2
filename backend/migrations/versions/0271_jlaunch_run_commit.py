"""jlaunch runs: the training repo commit a run cloned.

GrowBot training jobs now come from the training repo's jobs.toml, and each run clones
the exact commit that file was read at (owner, 2026-11-14: send new jobs without a box
update). The control server forgets its runs on a restart, so the mirror keeps the commit
for the run's page. Empty for a built-in job and for every run before this.

The public results page does not read it (`api/jlaunch_share.py` lists the fields it
serves), and the table's RLS (0152/0153) is unchanged.

Revision ID: 0271
Revises: 0270
Create Date: 2026-11-14
"""

from alembic import op

revision = "0271"
down_revision = "0270"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.jlaunch_runs ADD COLUMN commit text NOT NULL DEFAULT ''")


def downgrade() -> None:
    op.execute("ALTER TABLE app.jlaunch_runs DROP COLUMN commit")
