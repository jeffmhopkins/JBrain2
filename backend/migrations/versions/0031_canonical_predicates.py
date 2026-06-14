"""The canonical_predicates index (predicate canonicalization Phase 2).

The storage substrate for embedding-assisted predicate canonicalization
(docs/PREDICATE_CANONICALIZATION.md §3.3): one global reference row per canonical
predicate, carrying the descriptor we embed plus the registry metadata
(value_shape/kind/functional). Phase 3 cosine-matches an unknown predicate
against this index to canonicalize or propose a new one.

The table is created EMPTY — rows are upserted from the live schema registry by
the idempotent `sync_predicates` worker job (and embeddings backfilled there),
so the registry YAML stays the single source of truth instead of a frozen
snapshot pasted here. Global reference data like app.domains (every principal
reads), but self-extending, so writes are gated to the owner/system context.

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-14
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # vector(384) + HNSW cosine index mirror app.entities.summary_embedding
    # (0006); the pgvector extension already exists (created in 0003).
    op.execute(
        """
        CREATE TABLE app.canonical_predicates (
            canonical_name text PRIMARY KEY,
            -- the text we embed (a definition + shape hint, not the bare token)
            descriptor text NOT NULL,
            embedding vector(384),
            embedding_model text,
            value_shape text NOT NULL,
            kind text NOT NULL,
            functional boolean NOT NULL DEFAULT false,
            origin text NOT NULL DEFAULT 'seed'
                CHECK (origin IN ('seed', 'minted')),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX canonical_predicates_embedding_idx"
        " ON app.canonical_predicates USING hnsw (embedding vector_cosine_ops)"
    )

    op.execute("ALTER TABLE app.canonical_predicates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.canonical_predicates FORCE ROW LEVEL SECURITY")
    # Global reference data: every principal reads (app.domains precedent, 0001).
    op.execute(
        "CREATE POLICY canonical_predicates_read ON app.canonical_predicates"
        " FOR SELECT USING (true)"
    )
    # Self-extending: only the owner/system context seeds, mints, and embeds.
    op.execute(
        "CREATE POLICY canonical_predicates_insert ON app.canonical_predicates"
        " FOR INSERT WITH CHECK (app.is_owner())"
    )
    op.execute(
        "CREATE POLICY canonical_predicates_update ON app.canonical_predicates"
        " FOR UPDATE USING (app.is_owner()) WITH CHECK (app.is_owner())"
    )
    # SELECT for every reader; INSERT seeds/mints, UPDATE backfills embeddings.
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.canonical_predicates TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.canonical_predicates")
