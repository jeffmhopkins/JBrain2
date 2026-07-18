"""External corpus — store the word/cue-level transcript for the synced card tab.

`0133` deliberately kept only the searchable passage text (`external_source_chunks`) and dropped
the per-word timing as bloat. The `show_external_source` video-analysis card, though, has a synced
transcript tab (AudioTranscript) that needs the fine `{text, words:[{text, start_ms, end_ms}]}`
timing to highlight in step with playback — the same dict the live analyze_stream card renders.

Add a nullable `transcript jsonb` column to carry it. Populated by the `analyze_stream`
write-through (`persist_analysis`) from the analysis it already produced — zero extra compute,
and NULL for rows analysed before this (they render text-only until re-analysed). The column is on
the already-firewalled table, so no new RLS policy or isolation test is needed.

Revision ID: 0135
Revises: 0134
Create Date: 2026-07-18
"""

from alembic import op

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.external_sources ADD COLUMN transcript jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE app.external_sources DROP COLUMN transcript")
