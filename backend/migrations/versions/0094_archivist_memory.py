"""The archivist persona's cross-session memory (docs/EMAIL_ARCHIVIST_PLAN.md).

A single owner-only scratchpad document the archivist reads at session start and
rewrites as it makes filing/taxonomy decisions — the agent's own working notes, NOT
the owner's knowledge base (no domain, no notes/entities). Owner-only RLS mirrors
`generated_images`/`wiki_*` (`app.is_owner()` USING+CHECK, FORCE RLS). One row per
principal (the single owner), updated in place — so SELECT/INSERT/UPDATE/DELETE are
granted (unlike the immutable image-provenance rows).

Revision ID: 0094
Revises: 0093
Create Date: 2026-06-25
"""

from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.archivist_memory (
            principal_id text PRIMARY KEY,
            content text NOT NULL DEFAULT '',
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.archivist_memory ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.archivist_memory FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY archivist_memory_owner ON app.archivist_memory
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.archivist_memory TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.archivist_memory")
