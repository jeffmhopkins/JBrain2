"""Capture-location metadata on notes (Phase 2 step 4).

Stored verbatim from the client; owner-eyes metadata — the notes RLS policy
from 0002 already covers the rows, and Phase 7 scoped-token serialization
must exclude these columns from non-owner responses.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-10
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.notes
        ADD COLUMN latitude double precision,
        ADD COLUMN longitude double precision,
        ADD COLUMN location_accuracy_m real
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.notes
        DROP COLUMN latitude,
        DROP COLUMN longitude,
        DROP COLUMN location_accuracy_m
        """
    )
