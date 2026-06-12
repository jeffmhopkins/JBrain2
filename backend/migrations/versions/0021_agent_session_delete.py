"""Let the owner delete a Full Brain session and have it take its history with it.

Deleting a session cascades to its runs (and their steps) and its transcript;
episodes only lose their link (they're owner memory, kept). Renaming is just a
title UPDATE (already granted). Owner-only throughout — agent_sessions RLS is
unchanged; we only add the DELETE privilege and make the run-log FK cascade.

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-12
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # agent_runs.session_id was a plain FK; on a session delete it would block.
    # Recreate it as ON DELETE CASCADE so a deleted session takes its runs (and,
    # via their own cascade, their steps) with it.
    op.execute("ALTER TABLE app.agent_runs DROP CONSTRAINT agent_runs_session_id_fkey")
    op.execute(
        """
        ALTER TABLE app.agent_runs
        ADD CONSTRAINT agent_runs_session_id_fkey
        FOREIGN KEY (session_id) REFERENCES app.agent_sessions(id) ON DELETE CASCADE
        """
    )
    op.execute("GRANT DELETE ON app.agent_sessions TO jbrain_app")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON app.agent_sessions FROM jbrain_app")
    op.execute("ALTER TABLE app.agent_runs DROP CONSTRAINT agent_runs_session_id_fkey")
    op.execute(
        """
        ALTER TABLE app.agent_runs
        ADD CONSTRAINT agent_runs_session_id_fkey
        FOREIGN KEY (session_id) REFERENCES app.agent_sessions(id)
        """
    )
