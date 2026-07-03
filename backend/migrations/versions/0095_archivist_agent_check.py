"""Allow the `archivist` persona in the agent CHECK constraints.

Migration 0070 (agent_sessions) and 0093 (tasks) pinned `agent` to the then-closed
set ('curator', 'teacher', 'jerv'). The archivist persona shipped in code
(jbrain.agent.agents) but its name was never added to the DB constraints, so
creating an archivist session — or task — raised a CHECK violation while the other
personas worked. Widen both constraints to include 'archivist'. Owner-only metadata
tables, so RLS is unchanged. See docs/archive/EMAIL_ARCHIVIST_PLAN.md.

Revision ID: 0095
Revises: 0094
Create Date: 2026-06-25
"""

from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None

_OLD = "('curator', 'teacher', 'jerv')"
_NEW = "('curator', 'teacher', 'jerv', 'archivist')"


def upgrade() -> None:
    op.execute("ALTER TABLE app.agent_sessions DROP CONSTRAINT agent_sessions_agent_check")
    op.execute(
        f"ALTER TABLE app.agent_sessions ADD CONSTRAINT agent_sessions_agent_check "
        f"CHECK (agent IN {_NEW})"
    )
    # The tasks.agent CHECK was declared inline, so Postgres auto-named it tasks_agent_check.
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT tasks_agent_check")
    op.execute(f"ALTER TABLE app.tasks ADD CONSTRAINT tasks_agent_check CHECK (agent IN {_NEW})")


def downgrade() -> None:
    op.execute("ALTER TABLE app.agent_sessions DROP CONSTRAINT agent_sessions_agent_check")
    op.execute(
        f"ALTER TABLE app.agent_sessions ADD CONSTRAINT agent_sessions_agent_check "
        f"CHECK (agent IN {_OLD})"
    )
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT tasks_agent_check")
    op.execute(f"ALTER TABLE app.tasks ADD CONSTRAINT tasks_agent_check CHECK (agent IN {_OLD})")
