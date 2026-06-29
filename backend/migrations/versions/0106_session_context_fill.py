"""Per-session context fill: agent_sessions gains the last completed turn's
context_tokens + context_window, so reopening a chat restores its context-usage
meter at once instead of waiting for the next turn (token counts aren't in the
stored transcript, so the fill can't be recomputed from it).

Both nullable — null until a turn has reported usage; a pre-feature chat (or one
whose model never emitted a usage event) stays null and the meter simply waits for
the next turn. Owner-only is_owner() RLS is inherited from the table, unchanged.

Revision ID: 0106
Revises: 0105
Create Date: 2026-06-29
"""

from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.agent_sessions
            ADD COLUMN context_tokens integer,
            ADD COLUMN context_window integer
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.agent_sessions
            DROP COLUMN context_window,
            DROP COLUMN context_tokens
        """
    )
