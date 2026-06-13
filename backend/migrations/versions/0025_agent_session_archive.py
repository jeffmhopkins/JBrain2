"""Allow an `archived` agent-session status (docs/ASSISTANT.md "Sessions").

Archiving tidies a chat out of the live Chats list without deleting it or its
transcript — a third lifecycle state alongside `active` and `ended`. Widen the
status CHECK from migration 0015 to admit it.

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-13
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.agent_sessions DROP CONSTRAINT agent_sessions_status_check")
    op.execute(
        "ALTER TABLE app.agent_sessions ADD CONSTRAINT agent_sessions_status_check"
        " CHECK (status IN ('active', 'ended', 'archived'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.agent_sessions DROP CONSTRAINT agent_sessions_status_check")
    op.execute(
        "ALTER TABLE app.agent_sessions ADD CONSTRAINT agent_sessions_status_check"
        " CHECK (status IN ('active', 'ended'))"
    )
