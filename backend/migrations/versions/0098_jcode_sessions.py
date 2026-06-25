"""jcode_sessions: the owner-only launcher index for code-mode sessions.

The api's metadata mirror of the sandboxed coding sessions that live in the jcode
control server (docs/proposed/JCODE_PLAN.md, Wave J2). Holds no owner knowledge —
just the repo/branch/status the launcher lists and resumes. Owner-only RLS
(app.is_owner()), like generated_images / archivist_memory; no domain column,
because the sandbox touches no domain-scoped data.
"""

from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jcode_sessions (
            id text PRIMARY KEY,
            repo text NOT NULL DEFAULT '',
            branch text NOT NULL DEFAULT 'main',
            work_branch text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'ready',
            created_at timestamptz NOT NULL DEFAULT now(),
            last_active_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.jcode_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.jcode_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY jcode_sessions_owner ON app.jcode_sessions
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON app.jcode_sessions TO jbrain_app"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.jcode_sessions")
