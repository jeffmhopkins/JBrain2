"""Client-side capture time on notes (docs/ANALYSIS.md "Temporal model").

The offline outbox makes server receipt time (created_at) the wrong anchor
for resolving "today"/"last Tuesday" — and timestamptz normalizes away the
author's UTC offset, which the extraction prompt needs to state the anchor
in the author's local frame. So: the instant in captured_at, the frame in
capture_tz_offset_min. Nullable — pre-existing notes and clients fall back
to created_at in UTC.

The notes RLS policy from 0002 already covers the rows.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-10
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.notes
        ADD COLUMN captured_at timestamptz,
        ADD COLUMN capture_tz_offset_min integer
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.notes
        DROP COLUMN captured_at,
        DROP COLUMN capture_tz_offset_min
        """
    )
