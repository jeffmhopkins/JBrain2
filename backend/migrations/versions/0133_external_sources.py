"""External video corpus — one row per analysed video (EXTERNAL_VIDEO_INGESTION_PLAN.md, Phase A).

A durable home for YouTube (and other yt-dlp provider) video analyses: the summary, metadata,
frame thumbnails, and a source-level summary embedding. This is the "external ingested source"
store — third-party content the assistant can search, deliberately kept OUT of the notes/entity
graph (it is not a source of truth, #7). The searchable transcript/timeline passages live in the
sibling `external_source_chunks` table (0134).

Carries the standard `app.has_domain_scope(domain_code)` firewall (0002), defaulting to `general`
— public video content is never health/finance/location. `(provider, video_id)` is unique: the
`analyze_stream` write-through upserts with `ON CONFLICT DO NOTHING`, so a repeat analysis of the
same video is a no-op and the row doubles as a dedup ledger. `summary_embedding` is sized for
bge-small-en-v1.5 (384 dims) with an HNSW cosine index, matching `app.chunks`.

Revision ID: 0133
Revises: 0132
Create Date: 2026-07-18
"""

from alembic import op

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.external_sources (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            provider text NOT NULL DEFAULT 'youtube',
            video_id text NOT NULL,
            url text NOT NULL,
            title text,
            channel_id text,
            channel_name text,
            -- from yt-dlp upload_date (day precision); NULL when the provider omits it.
            published_at timestamptz,
            duration_s integer,
            duration_ms integer,
            summary text,
            -- source-level "which video" vector (bge-small-en-v1.5, 384 dims); the passage
            -- vectors live on external_source_chunks. Written by the embed_external_source job.
            summary_embedding vector(384),
            embedding_model text,
            -- which transcript the analysis used:
            -- 'captions:manual' | 'captions:auto' | 'whisper' | ''.
            transcript_source text,
            -- [{t_ms, caption, thumb_id}] for thumbnails-at-timestamp; NOT the per-word
            -- transcript (that text is the searchable chunks — storing it here too is bloat).
            frames jsonb,
            -- pipeline provenance (the router spec string that produced the analysis).
            tool text,
            origin text NOT NULL DEFAULT 'adhoc'
                CHECK (origin IN ('adhoc', 'task')),
            status text NOT NULL DEFAULT 'analyzing'
                CHECK (status IN ('analyzing', 'done', 'unavailable')),
            last_error text,
            analyzed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            domain_code text NOT NULL DEFAULT 'general' REFERENCES app.domains(code),
            UNIQUE (provider, video_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX external_sources_status_idx ON app.external_sources (status, created_at)"
    )
    op.execute(
        "CREATE INDEX external_sources_channel_idx"
        " ON app.external_sources (channel_id, published_at DESC)"
    )
    op.execute(
        "CREATE INDEX external_sources_summary_embedding_idx"
        " ON app.external_sources USING hnsw (summary_embedding vector_cosine_ops)"
    )

    op.execute("ALTER TABLE app.external_sources ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.external_sources FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY external_sources_domain ON app.external_sources
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    # DELETE because a re-analysis (and its blob reaper) rebuilds a source's data wholesale.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.external_sources TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.external_sources")
