"""`turn_attachments.native_clip` — a chat video's clip, cached for jerv's own context.

A short clip a video-capable chat model can see is sent INSIDE the conversation, pinned at the
turn it was attached so the engine's prefix cache holds it (NATIVE_VIDEO_PLAN §5). That only
works if every later turn sends the same bytes and the same note, so the transcoded clip (a
blob id), its length, its token charge and its transcript are kept here once and re-read,
never re-transcoded or re-transcribed. A clip too long to inline records that too, so it is
probed once.

Separate from `analysis` (0084), which is analyze_video's cache and doubles as the thumbnail
firewall's frame list: an inline clip has no frames, and the two are written at different
times by different paths. Rides the table's existing RLS policies and grants.

Revision ID: 0221
Revises: 0220
Create Date: 2026-10-06
"""

from alembic import op

revision = "0221"
down_revision = "0220"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.turn_attachments ADD COLUMN native_clip jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE app.turn_attachments DROP COLUMN native_clip")
