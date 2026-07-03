"""Sub-agent spawning lineage: parent/depth/no-memory on sessions, parent_run_id +
the `subagent` run kind, and the three web-sandboxed personas in the agent CHECKs.

Wave S1.1 of docs/archive/SUBAGENT_SPAWNING_PLAN.md. All net-new structural machinery for
`jerv`'s bounded fan of research/review/summarize children:

- `agent_sessions` gains `parent_session_id` (self-FK, CASCADE so a deleted parent
  takes its sub-agents with it — children are sub-state of the parent turn, never
  orphaned into the top-level list), `depth` (SMALLINT, DB-CHECKed to 0..2 so the
  two-sub-agent-layer cap is structural at the table, not just in the loop), and
  `no_memory` (the sandbox flag the spawn helper sets so a child's turn is never
  episodically appended).
- `runs` gains `parent_run_id` (self-FK, SET NULL to preserve a child's audit row)
  for the tree cost rollup, and its `kind` CHECK is widened to admit `'subagent'`.
- The `agent` CHECK on both `agent_sessions` and `tasks` is widened to admit
  `research`/`review`/`summarize`, or a child session INSERT would fail outright.

Owner-only metadata tables (is_owner() RLS); the new columns inherit that posture,
so RLS is unchanged. Roots default cleanly (`depth=0`, `parent_session_id=NULL`,
`no_memory=false`).

Revision ID: 0105
Revises: 0104
Create Date: 2026-06-28
"""

from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None

_AGENT_OLD = "('curator', 'teacher', 'jerv', 'archivist')"
_AGENT_NEW = "('curator', 'teacher', 'jerv', 'archivist', 'research', 'review', 'summarize')"


def upgrade() -> None:
    # --- agent_sessions: lineage + sandbox flag -------------------------------
    op.execute(
        """
        ALTER TABLE app.agent_sessions
            ADD COLUMN parent_session_id uuid
                REFERENCES app.agent_sessions(id) ON DELETE CASCADE,
            ADD COLUMN depth smallint NOT NULL DEFAULT 0
                CHECK (depth BETWEEN 0 AND 2),
            ADD COLUMN no_memory boolean NOT NULL DEFAULT false
        """
    )

    # --- runs: parent link + the subagent kind --------------------------------
    op.execute(
        "ALTER TABLE app.runs"
        " ADD COLUMN parent_run_id uuid REFERENCES app.runs(id) ON DELETE SET NULL"
    )
    # The kind CHECK was declared inline (migration 0037), so Postgres auto-named
    # it runs_kind_check. Drop + re-add to admit 'subagent'.
    op.execute("ALTER TABLE app.runs DROP CONSTRAINT runs_kind_check")
    op.execute(
        "ALTER TABLE app.runs ADD CONSTRAINT runs_kind_check"
        " CHECK (kind IN ('agent', 'integration', 'pipeline', 'subagent'))"
    )

    # --- widen the agent CHECKs for the three web-sandboxed personas ----------
    op.execute("ALTER TABLE app.agent_sessions DROP CONSTRAINT agent_sessions_agent_check")
    op.execute(
        f"ALTER TABLE app.agent_sessions ADD CONSTRAINT agent_sessions_agent_check "
        f"CHECK (agent IN {_AGENT_NEW})"
    )
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT tasks_agent_check")
    op.execute(
        f"ALTER TABLE app.tasks ADD CONSTRAINT tasks_agent_check CHECK (agent IN {_AGENT_NEW})"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT tasks_agent_check")
    op.execute(
        f"ALTER TABLE app.tasks ADD CONSTRAINT tasks_agent_check CHECK (agent IN {_AGENT_OLD})"
    )
    op.execute("ALTER TABLE app.agent_sessions DROP CONSTRAINT agent_sessions_agent_check")
    op.execute(
        f"ALTER TABLE app.agent_sessions ADD CONSTRAINT agent_sessions_agent_check "
        f"CHECK (agent IN {_AGENT_OLD})"
    )

    op.execute("ALTER TABLE app.runs DROP CONSTRAINT runs_kind_check")
    op.execute(
        "ALTER TABLE app.runs ADD CONSTRAINT runs_kind_check"
        " CHECK (kind IN ('agent', 'integration', 'pipeline'))"
    )
    op.execute("ALTER TABLE app.runs DROP COLUMN parent_run_id")

    op.execute(
        """
        ALTER TABLE app.agent_sessions
            DROP COLUMN no_memory,
            DROP COLUMN depth,
            DROP COLUMN parent_session_id
        """
    )
