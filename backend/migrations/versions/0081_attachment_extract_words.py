"""Per-word transcript data for the karaoke transcript UI.

The audio-transcript component (docs/mocks/audio-transcript-approved.html) needs
each word's place in the clip + how sure the model is. Store that as a nullable
JSONB array on the existing app.attachment_extracts row (alongside the plain
`text`): [{"text", "start_ms", "end_ms", "confidence"}, ...]. Display-only — the
searchable chunks still use `text`. Existing/OCR/caption rows keep words = NULL;
only transcript rows (and only once re-transcribed) populate it.

Rides the existing app.attachment_extracts RLS policy and grants (no new table).

Revision ID: 0081
Revises: 0080
Create Date: 2026-06-22
"""

from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.attachment_extracts ADD COLUMN words jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE app.attachment_extracts DROP COLUMN words")
