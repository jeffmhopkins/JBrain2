"""Admit the 'transcript' attachment-extract kind.

The audio transcription chain (docs/archive/WHISPER_TRANSCRIPTION_PLAN.md) writes a
kind='transcript' row to app.attachment_extracts — the audio sibling of the
'ocr'/'caption' vision products. The kind CHECK (migration 0011) is an explicit
allowlist, so the new kind needs admitting here.

Rides the existing app.attachment_extracts RLS policy and grants (no new table).

Revision ID: 0079
Revises: 0078
Create Date: 2026-06-22
"""

from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.attachment_extracts DROP CONSTRAINT attachment_extracts_kind_check")
    op.execute(
        "ALTER TABLE app.attachment_extracts ADD CONSTRAINT attachment_extracts_kind_check"
        " CHECK (kind IN ('ocr', 'caption', 'transcript'))"
    )


def downgrade() -> None:
    # Clear the new kind before narrowing, or the re-add would fail.
    op.execute("DELETE FROM app.attachment_extracts WHERE kind = 'transcript'")
    op.execute("ALTER TABLE app.attachment_extracts DROP CONSTRAINT attachment_extracts_kind_check")
    op.execute(
        "ALTER TABLE app.attachment_extracts ADD CONSTRAINT attachment_extracts_kind_check"
        " CHECK (kind IN ('ocr', 'caption'))"
    )
