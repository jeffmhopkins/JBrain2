"""Capture-time UTC offset on notes, so the extraction anchor is the note's
LOCAL date/time.

A timestamptz stores an instant in UTC and discards the writer's offset on
read-back, so created_at alone cannot answer "what calendar day was it where
the note was written?" — which is exactly the anchor every relative phrase
("today", "in 3 months") resolves against. We persist the client's offset
(minutes east of UTC, the negation of JS getTimezoneOffset) alongside the
instant; the pipeline reconstructs local wall-clock from the pair. Nullable:
pre-Phase-3 rows and server-defaulted captures have no client offset and fall
back to the stored (UTC) instant.

Rides the existing app.notes RLS policy (no new table); proven by the notes
RLS isolation test.

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
    op.execute("ALTER TABLE app.notes ADD COLUMN tz_offset_minutes integer")


def downgrade() -> None:
    op.execute("ALTER TABLE app.notes DROP COLUMN tz_offset_minutes")
