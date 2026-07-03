"""Agent selection: the persona an agent session runs as.

Full Brain mode lets the owner start a chat as one of several agents
(docs/reference/ASSISTANT.md "Agent selection"). The selection is stored on the session as
`agent` — which system prompt frames the turn, which tools it may call, and
whether it reads the knowledge base. A CHECK pins it to the closed code-defined
set so a malformed value can never reach the turn loop; it defaults to the Full
Brain `curator`, so every existing session keeps its current behaviour — fully
backward compatible. `agent_sessions` stays owner-only metadata, so RLS is
unchanged (the is_owner() policy from migration 0015).

Revision ID: 0070
Revises: 0069
Create Date: 2026-06-19
"""

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.agent_sessions ADD COLUMN agent text NOT NULL DEFAULT 'curator'")
    op.execute(
        "ALTER TABLE app.agent_sessions ADD CONSTRAINT agent_sessions_agent_check "
        "CHECK (agent IN ('curator', 'teacher', 'jerv'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.agent_sessions DROP CONSTRAINT agent_sessions_agent_check")
    op.execute("ALTER TABLE app.agent_sessions DROP COLUMN agent")
