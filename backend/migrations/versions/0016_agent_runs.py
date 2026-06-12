"""Agent run log: one row per turn-loop execution, plus its steps.

The audit and (later) training trace for the agent loop (docs/ASSISTANT_PLAN.md
P4.4). Owner-only metadata — a run records which session it served and what it
spent, never note content — so RLS is the is_owner() pattern, like app.jobs. In
Phase 5 these become the workflow engine's `runs`; the shape is forward-compatible.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-12
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.agent_runs (
            id uuid PRIMARY KEY,
            session_id uuid NOT NULL REFERENCES app.agent_sessions(id),
            status text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'done', 'error')),
            stop_reason text,
            step_count integer NOT NULL DEFAULT 0,
            cost_tokens bigint NOT NULL DEFAULT 0,
            prompt_version text NOT NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX agent_runs_session_idx ON app.agent_runs (session_id, started_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE app.agent_steps (
            id uuid PRIMARY KEY,
            run_id uuid NOT NULL REFERENCES app.agent_runs(id) ON DELETE CASCADE,
            idx integer NOT NULL,
            kind text NOT NULL,
            name text NOT NULL,
            tool_version integer,
            ok boolean NOT NULL,
            cost_tokens bigint NOT NULL DEFAULT 0,
            at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX agent_steps_run_idx ON app.agent_steps (run_id, idx)")

    for table in ("agent_runs", "agent_steps"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_owner ON app.{table}
            USING (app.is_owner())
            WITH CHECK (app.is_owner())
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON app.{table} TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.agent_steps")
    op.execute("DROP TABLE app.agent_runs")
