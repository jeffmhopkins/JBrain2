"""Cache an analyze_video result on its chat attachment.

jerv's `analyze_video` tool (docs/VIDEO_ANALYSIS_PLAN.md) computes a video analysis
inline and now caches it on the `app.turn_attachments` row so a re-ask is free and —
the security point — the per-frame thumbnails become servable: the thumbnail endpoint
validates a requested `thumb_id` (a blob sha) against THIS row's stored frame list
under the attachment's domain firewall, so a raw blob is never served by sha (which
would bypass the firewall — invariant #3). Shape: {summary, duration_ms,
frames:[{t_ms, caption, thumb_id}], transcript:{text, words}|null}.

Rides the existing app.turn_attachments RLS policy + grants (no new table).

Revision ID: 0085
Revises: 0084
Create Date: 2026-06-22
"""

from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.turn_attachments ADD COLUMN analysis jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE app.turn_attachments DROP COLUMN analysis")
