"""jlaunch: the owner's archived job types.

The owner asked (2026-11-14) to archive job types they're done with (the older math runs, a
superseded policy-training job) so the Jobs tab lists only what's current; an Archived tab
holds the rest and restores one. The job types themselves are code-defined in the launcher,
so archiving is the owner's view of them, kept here: one row per archived spec name. A
spec's runs are untouched.

Owner-only RLS like `jlaunch_runs` (0152): no share link, member or capability session
reads or writes it.

Revision ID: 0270
Revises: 0269
Create Date: 2026-11-14
"""

from alembic import op

revision = "0270"
down_revision = "0269"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jlaunch_spec_archive (
            spec_name text PRIMARY KEY,
            archived_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.jlaunch_spec_archive ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.jlaunch_spec_archive FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY jlaunch_spec_archive_owner ON app.jlaunch_spec_archive
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.jlaunch_spec_archive TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.jlaunch_spec_archive")
