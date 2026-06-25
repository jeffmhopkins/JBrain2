"""Tasks: saved prompts that spawn an agent session on a schedule or on demand.

`tasks` and `task_runs` are owner-only metadata (RLS `is_owner()`, like
`agent_sessions`, migration 0015). A run points at the `agent_session` it produced
(ON DELETE SET NULL so deleting a chat doesn't break run history) and cascades when
its task is deleted. The persona/schedule sets are pinned by CHECKs so a malformed
value can never reach the runner. See docs/mocks/tasks-launcher-README.md.

Revision ID: 0093
Revises: 0092
Create Date: 2026-06-24
"""

from alembic import op

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.tasks (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            name text NOT NULL DEFAULT '',
            prompt text NOT NULL,
            agent text NOT NULL DEFAULT 'jerv'
                CHECK (agent IN ('curator', 'teacher', 'jerv')),
            domain_scopes text[] NOT NULL DEFAULT '{}',
            schedule_kind text NOT NULL DEFAULT 'on_demand'
                CHECK (schedule_kind IN ('on_demand', 'once', 'repeat')),
            schedule_freq text
                CHECK (schedule_freq IS NULL OR schedule_freq IN ('daily', 'weekdays', 'weekly')),
            schedule_days int[] NOT NULL DEFAULT '{}',
            schedule_time text,
            run_at timestamptz,
            timezone text NOT NULL DEFAULT 'UTC',
            enabled boolean NOT NULL DEFAULT true,
            notify_push boolean NOT NULL DEFAULT true,
            home_card boolean NOT NULL DEFAULT true,
            next_run_at timestamptz,
            last_run_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # The scheduler claims enabled, due rows — index that access path.
    op.execute(
        "CREATE INDEX tasks_due_idx ON app.tasks (next_run_at)"
        " WHERE enabled AND next_run_at IS NOT NULL"
    )
    op.execute("ALTER TABLE app.tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.tasks FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tasks_owner ON app.tasks
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.tasks TO jbrain_app")

    op.execute(
        """
        CREATE TABLE app.task_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES app.tasks(id) ON DELETE CASCADE,
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            session_id uuid REFERENCES app.agent_sessions(id) ON DELETE SET NULL,
            run_id uuid,
            status text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'done', 'error')),
            trigger text NOT NULL DEFAULT 'schedule'
                CHECK (trigger IN ('schedule', 'manual')),
            summary text NOT NULL DEFAULT '',
            error text,
            step_count int NOT NULL DEFAULT 0,
            cost_tokens int NOT NULL DEFAULT 0,
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX task_runs_task_idx ON app.task_runs (task_id, started_at DESC)")
    op.execute("ALTER TABLE app.task_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.task_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY task_runs_owner ON app.task_runs
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.task_runs TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.task_runs")
    op.execute("DROP TABLE app.tasks")
