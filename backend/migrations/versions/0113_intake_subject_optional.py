"""Make intake_links.subject_id optional — general / no-person intake.

Intake was built for facts attributed to an existing subject (a relative's medical
history), so subject_id was NOT NULL. But many valid collections aren't about a
specific person (a favorite recipe, general info), and forcing a subject there made
the assistant invent a bogus id that only failed at the mint FK. Dropping NOT NULL
lets a link carry no subject; the FK still holds when one IS set, and the domain (not
the subject) is what the firewall isolates — so this doesn't touch RLS.
"""

from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.intake_links ALTER COLUMN subject_id DROP NOT NULL")


def downgrade() -> None:
    # Backfilling a subject for rows that never had one is impossible, so the
    # downgrade only re-imposes NOT NULL (it will fail if any null rows exist).
    op.execute("ALTER TABLE app.intake_links ALTER COLUMN subject_id SET NOT NULL")
