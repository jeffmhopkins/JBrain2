"""A provenance stamp so grabbed/fetched stills reuse `generated_images` without
pretending to be generations.

`generated_images` is the owner-only chat-image artifact table (migration 0077). The
video/image inspection tools (VIDEO_IMAGE_TOOLS_PLAN.md) make a video frame
(`grab_frame`) and a fetched web image (`fetch_image`) first-class chat images so
`analyze_image` can resolve them by id — but they are not *generated*. Rather than
overload the behaviour-bearing `kind` column (`kind == 'edit'` drives the before/after
card view and the `/source` route), this adds a nullable `provenance` column:

  NULL         a real generation/edit (the existing rows and the image-gen tools)
  'ffmpeg'     a still grabbed from a video at a timestamp
  'web_fetch'  an image fetched from a URL

The gallery lists only `provenance IS NULL` rows (a fetched product photo is not a
render the owner made), while `repo.get(id)` stays provenance-agnostic so the in-chat
tools resolve any chat image by id. Nullable, owner-only like the rest of the row, no
firewall column.

Revision ID: 0139
Revises: 0138
Create Date: 2026-07-19
"""

from alembic import op

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL for every existing row (all are generations/edits); the grab/fetch tools set
    # 'ffmpeg'/'web_fetch'. Free-text (no CHECK) — provenance is descriptive, not a
    # behaviour switch, so it needs no constraint and admits a new source without a
    # migration, unlike `kind`.
    op.execute("ALTER TABLE app.generated_images ADD COLUMN provenance text")


def downgrade() -> None:
    op.execute("ALTER TABLE app.generated_images DROP COLUMN IF EXISTS provenance")
