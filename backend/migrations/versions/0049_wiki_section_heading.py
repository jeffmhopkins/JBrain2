"""Add `wiki_sections.heading` (Phase-6 Wave C2b fix).

A type guide defines several sections in the SAME domain (Person → Early life / Career /
Personal life, all `general`). Without a heading the builder keyed find-or-create on
(article, domain) alone, collapsing them onto one row so all but the last clobbered each other.
The heading is the section identity within a domain, and it is what the reader renders — so it
is persisted here and the builder keys sections on (article_id, domain_code, heading).

Revision ID: 0049
Revises: 0048
Create Date: 2026-06-17
"""

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.wiki_sections ADD COLUMN heading text NOT NULL DEFAULT ''")
    # The find-or-create key for a top-level section: one row per (article, domain, heading).
    op.execute(
        "CREATE INDEX wiki_sections_heading_idx ON app.wiki_sections"
        " (article_id, domain_code, heading)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.wiki_sections_heading_idx")
    op.execute("ALTER TABLE app.wiki_sections DROP COLUMN heading")
