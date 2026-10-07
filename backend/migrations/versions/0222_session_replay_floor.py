"""`agent_sessions.replay_floor_seq` — where a chat's replayed tool results stop.

jerv's history is rebuilt from the transcript with each earlier turn's tool calls AND their
results (docs/plans/TOOL_RESULT_REPLAY_PLAN.md), inside a token budget. Turns before this
boundary keep their calls but replay a one-line stub instead of the result. The boundary is
stored, not recomputed, because the replay must render byte-identically turn over turn for the
engine's prefix cache — across a restart, another process, or the chat-pair slot swap — and it
only ever moves forward. A re-scope moves it past every turn so far: results read under the old
scope never replay into the new one.

Rides the table's existing RLS policies and grants.

Revision ID: 0222
Revises: 0221
Create Date: 2026-10-06
"""

from alembic import op

revision = "0222"
down_revision = "0221"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.agent_sessions ADD COLUMN replay_floor_seq bigint NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.agent_sessions DROP COLUMN replay_floor_seq")
