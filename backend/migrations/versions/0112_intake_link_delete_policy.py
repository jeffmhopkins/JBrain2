"""A DELETE policy on intake_links so the purge can hard-delete a link (W4).

The W1 policies (0108) cover SELECT/INSERT/UPDATE but not DELETE, so under FORCE RLS a
DELETE matched no rows and the purge couldn't remove a link. A full-owner DELETE policy
fixes that; the cascade to intake_sessions/intake_submissions (and their transcripts)
runs as a referential action, which bypasses the children's RLS, so no child DELETE
policy is needed. Owner/system only — a stranger's intake principal can never delete.
"""

from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE POLICY intake_links_delete ON app.intake_links FOR DELETE"
        " USING (app.is_full_owner())"
    )


def downgrade() -> None:
    op.execute("DROP POLICY intake_links_delete ON app.intake_links")
