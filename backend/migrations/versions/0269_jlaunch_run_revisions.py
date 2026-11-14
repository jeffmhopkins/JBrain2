"""jlaunch runs: a per-job revision number and an owner note.

Runs of the same job all read alike in the Runs list (owner, 2026-11-14: "a way to keep
track of previous revisions"). `revision` numbers each job's runs v1, v2, … in launch order
and never shifts when an older run is deleted; `note` is the owner's own line on what a run
was for ("lower lr", "after the hip fix"). Existing rows are numbered by `created_at`.

The public results page does not read either column (`api/jlaunch_share.py` lists the
fields it serves), and the table's RLS (0152/0153) is unchanged.

Revision ID: 0269
Revises: 0268
Create Date: 2026-11-14
"""

from alembic import op

revision = "0269"
down_revision = "0268"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.jlaunch_runs ADD COLUMN revision integer")
    op.execute("ALTER TABLE app.jlaunch_runs ADD COLUMN note text NOT NULL DEFAULT ''")
    op.execute(
        """
        UPDATE app.jlaunch_runs r SET revision = n.rev
        FROM (
            SELECT id, row_number() OVER (PARTITION BY spec_name ORDER BY created_at, id) AS rev
            FROM app.jlaunch_runs
        ) n
        WHERE r.id = n.id
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.jlaunch_runs DROP COLUMN note")
    op.execute("ALTER TABLE app.jlaunch_runs DROP COLUMN revision")
