"""Add `entities.image_sha` (Phase-6 profile-image chain).

The owner sets an entity's profile image in the entity view; the bytes live in the content-
addressed blob store and the entity row keeps only their sha256. The builder copies this onto
`wiki_articles.image_sha` (the column added in 0045) so a scoped reader never reads the single-
domain `entities` row across the firewall — the article shell carries the denormalized image, the
same pattern as `title`/`kind`/`lead_summary`. The image is owner metadata, not a claim, so it
rides the existing domain-scoped RLS on `entities` (no new table, no new policy).

Revision ID: 0052
Revises: 0051
Create Date: 2026-06-17
"""

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.entities ADD COLUMN image_sha text")


def downgrade() -> None:
    op.execute("ALTER TABLE app.entities DROP COLUMN image_sha")
