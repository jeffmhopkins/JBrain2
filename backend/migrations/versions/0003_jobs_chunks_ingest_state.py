"""Job queue, ingestion state, and searchable chunks (Phase 2 steps 1-2).

`jobs` is a system table, not domain data: payloads carry row IDs only, never
note content, so a single owner-only policy suffices — the worker runs with an
owner-kind system SessionContext (see jbrain.queue.SYSTEM_CTX).

`chunks` carries the standard app.has_domain_scope(domain_code) firewall from
0002. The embedding column is sized for bge-small-en-v1.5 (384 dims, Step 3);
pgvector ships in the timescaledb-ha image and migrate runs as superuser, so
CREATE EXTENSION is safe here.

Existing notes default to ingest_state='pending' — that is the backfill
marker the worker's startup scan keys on.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-10
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
        CREATE TABLE app.jobs (
            id uuid PRIMARY KEY,
            kind text NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}',
            status text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'done', 'failed')),
            attempts int NOT NULL DEFAULT 0,
            max_attempts int NOT NULL DEFAULT 5,
            run_after timestamptz NOT NULL DEFAULT now(),
            locked_at timestamptz,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX jobs_claim_idx ON app.jobs (status, run_after)")
    op.execute("ALTER TABLE app.jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.jobs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY jobs_system ON app.jobs
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.jobs TO jbrain_app")

    op.execute(
        """
        ALTER TABLE app.notes
        ADD COLUMN ingest_state text NOT NULL DEFAULT 'pending'
            CHECK (ingest_state IN ('pending', 'processing', 'indexed', 'failed')),
        ADD COLUMN indexed_at timestamptz
        """
    )

    op.execute(
        """
        CREATE TABLE app.chunks (
            id uuid PRIMARY KEY,
            note_id uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
            attachment_id uuid REFERENCES app.attachments(id) ON DELETE CASCADE,
            domain_code text NOT NULL REFERENCES app.domains(code),
            granularity text NOT NULL CHECK (granularity IN ('paragraph', 'section')),
            seq int NOT NULL,
            char_start int,
            char_end int,
            source_kind text NOT NULL DEFAULT 'note'
                CHECK (source_kind IN ('note', 'text-layer', 'ocr', 'transcript', 'caption')),
            source_anchor text,
            text text NOT NULL,
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
            embedding vector(384),
            embedding_model text
        )
        """
    )
    op.execute("CREATE INDEX chunks_note_idx ON app.chunks (note_id)")
    op.execute("CREATE INDEX chunks_tsv_idx ON app.chunks USING GIN (tsv)")
    op.execute(
        "CREATE INDEX chunks_embedding_idx ON app.chunks USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("ALTER TABLE app.chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.chunks FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY chunks_domain ON app.chunks
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    # DELETE because re-ingestion replaces a note's chunks wholesale.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.chunks TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.chunks")
    op.execute("ALTER TABLE app.notes DROP COLUMN ingest_state, DROP COLUMN indexed_at")
    op.execute("DROP TABLE app.jobs")
