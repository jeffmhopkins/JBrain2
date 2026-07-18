"""External video corpus — searchable, time-stamped passages (EXTERNAL_VIDEO_INGESTION_PLAN.md).

The retrieval surface for the external-source corpus: one row per time-coherent passage of a
video's fused timeline (frame captions + spoken utterances), produced by the timeline windower —
NOT `chunker.chunk_text`, which has no notion of time. Each row carries a real millisecond offset
(`t_ms`) so a search hit deep-links to the moment in the video, clean marker-free prose (so the
`[mm:ss]`/`(frame)` scaffolding never pollutes FTS or the embedding), a `tsv` for the keyword leg,
and a bge-small-en-v1.5 embedding for the dense leg — the same hybrid `chunks` backs.

Parallel to `app.chunks` but with NO note FK: an external video must never mint a note (the trust
boundary), so isolation is structural — the graph search legs physically cannot reach these rows
and `search_external` physically cannot reach `app.chunks`. One granularity (time windows), so
`seq` is a single monotonic counter and `UNIQUE(source_id, seq)` holds without a granularity column.

Revision ID: 0134
Revises: 0133
Create Date: 2026-07-18
"""

from alembic import op

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.external_source_chunks (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id uuid NOT NULL REFERENCES app.external_sources(id) ON DELETE CASCADE,
            seq int NOT NULL,
            -- real ms offset of the window's first timeline entry (for the deep-link).
            t_ms int NOT NULL,
            -- clean prose with the timeline markers stripped (see the timeline windower).
            text text NOT NULL,
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
            embedding vector(384),
            embedding_model text,
            domain_code text NOT NULL DEFAULT 'general' REFERENCES app.domains(code),
            UNIQUE (source_id, seq)
        )
        """
    )
    op.execute(
        "CREATE INDEX external_source_chunks_tsv_idx"
        " ON app.external_source_chunks USING GIN (tsv)"
    )
    op.execute(
        "CREATE INDEX external_source_chunks_embedding_idx"
        " ON app.external_source_chunks USING hnsw (embedding vector_cosine_ops)"
    )

    op.execute("ALTER TABLE app.external_source_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.external_source_chunks FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY external_source_chunks_domain ON app.external_source_chunks
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    # DELETE because a re-analysis replaces a source's chunks wholesale (the chunks pattern).
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.external_source_chunks TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.external_source_chunks")
