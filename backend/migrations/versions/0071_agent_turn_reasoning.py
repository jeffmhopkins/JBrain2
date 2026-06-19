"""Persist a turn's reasoning trace.

A reasoning model (gpt-oss/GLM via the local gateway) emits a thinking trace
alongside its answer; the PWA shows it in a collapsible "thinking" disclosure. Store
it on the assistant `agent_turns` row so reopening a session replays the collapsed
"Thought for Ns" the same way the answer + Worked steps replay. Nullable with a ''
default — every existing row and every non-reasoning turn is simply empty, fully
backward compatible. `agent_turns` stays owner-only, so RLS is unchanged (the
is_owner() policy from migration 0015).

Revision ID: 0071
Revises: 0070
Create Date: 2026-06-19
"""

from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.agent_turns ADD COLUMN reasoning text NOT NULL DEFAULT ''")


def downgrade() -> None:
    op.execute("ALTER TABLE app.agent_turns DROP COLUMN reasoning")
