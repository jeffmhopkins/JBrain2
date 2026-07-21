"""External corpus: store the uploader's own video description + make it searchable.

The channel-authored description (yt-dlp's `description` — the text the uploader writes
under the video) is resolved at analysis time but was discarded. Persist it so a library
video carries what its channel *said about itself*, alongside the machine summary and the
spoken transcript, and give it its own source-level embedding so it is a semantic hit
target in `search_external_video` (mirroring `summary` / `summary_embedding`).

`description_embedding` is sized for bge-small-en-v1.5 (384 dims) with an HNSW cosine
index, matching `summary_embedding` and `app.chunks`. Row-level security already covers
`external_sources` (0133), so the new columns inherit the `external`-domain firewall with
no policy change. Written by the `embed_external_source` job; NULLed on re-analysis so it
re-fills for the refreshed description.

Revision ID: 0145
Revises: 0144
Create Date: 2026-07-20
"""

from alembic import op

revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.external_sources ADD COLUMN description text")
    op.execute("ALTER TABLE app.external_sources ADD COLUMN description_embedding vector(384)")
    op.execute(
        "CREATE INDEX external_sources_description_embedding_idx"
        " ON app.external_sources USING hnsw (description_embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.external_sources_description_embedding_idx")
    op.execute("ALTER TABLE app.external_sources DROP COLUMN IF EXISTS description_embedding")
    op.execute("ALTER TABLE app.external_sources DROP COLUMN IF EXISTS description")
