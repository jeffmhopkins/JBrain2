"""Add `wiki_articles.kind` (Phase-6 Wave C3 — search wiki leg).

The search wiki leg + the landing render an entity-type disc for each article. The kind lives on
the single-domain-RLS `entities` row, which a scoped reader (and the owner-visible article shell)
must not read across the firewall — so, like `title`/`image_sha`/`lead_summary`, the kind is
denormalized onto the article at build (system-scoped) and read from there.

Revision ID: 0050
Revises: 0049
Create Date: 2026-06-17
"""

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.wiki_articles ADD COLUMN kind text NOT NULL DEFAULT ''")


def downgrade() -> None:
    op.execute("ALTER TABLE app.wiki_articles DROP COLUMN kind")
