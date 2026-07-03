"""Admit the 'video_analysis' attachment-extract kind + its structured column.

The analyze_video chain (docs/archive/VIDEO_ANALYSIS_PLAN.md) writes one
kind='video_analysis' row to app.attachment_extracts — the video sibling of the
'ocr'/'caption'/'transcript' products. `text` holds the reduce-step summary (so it
chunks and becomes searchable like the others); the per-frame timeline
({t_ms, caption, thumb_id}, thumbs are blob ids — invariant #9) and the fused
transcript live in a new nullable `analysis` jsonb column alongside the existing
`words`. The summary also chunks, so app.chunks.source_kind admits the new kind too.

Both kind allowlists are explicit CHECKs (migrations 0011 / 0003+0014), so the new
kind is admitted in each. Rides the existing RLS policies + grants (no new table).

Revision ID: 0084
Revises: 0083
Create Date: 2026-06-22
"""

from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None

_CHUNK_KINDS_WITH = (
    "('note', 'text-layer', 'ocr', 'transcript', 'caption', 'derived', 'video_analysis')"
)
_CHUNK_KINDS_WITHOUT = "('note', 'text-layer', 'ocr', 'transcript', 'caption', 'derived')"


def upgrade() -> None:
    op.execute("ALTER TABLE app.attachment_extracts DROP CONSTRAINT attachment_extracts_kind_check")
    op.execute(
        "ALTER TABLE app.attachment_extracts ADD CONSTRAINT attachment_extracts_kind_check"
        " CHECK (kind IN ('ocr', 'caption', 'transcript', 'video_analysis'))"
    )
    op.execute("ALTER TABLE app.attachment_extracts ADD COLUMN analysis jsonb")
    op.execute("ALTER TABLE app.chunks DROP CONSTRAINT chunks_source_kind_check")
    op.execute(
        "ALTER TABLE app.chunks ADD CONSTRAINT chunks_source_kind_check"
        f" CHECK (source_kind IN {_CHUNK_KINDS_WITH})"
    )


def downgrade() -> None:
    # Clear the new kind from both tables before narrowing, or the re-add would fail.
    op.execute("DELETE FROM app.chunks WHERE source_kind = 'video_analysis'")
    op.execute("ALTER TABLE app.chunks DROP CONSTRAINT chunks_source_kind_check")
    op.execute(
        "ALTER TABLE app.chunks ADD CONSTRAINT chunks_source_kind_check"
        f" CHECK (source_kind IN {_CHUNK_KINDS_WITHOUT})"
    )
    op.execute("DELETE FROM app.attachment_extracts WHERE kind = 'video_analysis'")
    op.execute("ALTER TABLE app.attachment_extracts DROP COLUMN analysis")
    op.execute("ALTER TABLE app.attachment_extracts DROP CONSTRAINT attachment_extracts_kind_check")
    op.execute(
        "ALTER TABLE app.attachment_extracts ADD CONSTRAINT attachment_extracts_kind_check"
        " CHECK (kind IN ('ocr', 'caption', 'transcript'))"
    )
