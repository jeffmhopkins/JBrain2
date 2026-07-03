"""Generalize the agent run log into the workflow engine's unified run log.

`agent_runs`/`agent_steps` become `runs`/`run_steps` IN PLACE (a RENAME, not a
copy) so the same tables can also hold integration/pipeline runs without breaking
the agent (docs/archive/WORKFLOW_ENGINE_PLAN.md §3, §5 Track A). A rename is chosen over a
new table specifically so the dependent FKs from `agent_episodes` and
`agent_turns` (which reference `agent_runs.id`) follow the table
automatically — Postgres rewrites those constraints to point at the renamed table,
so no repoint is needed and no agent history is orphaned.

The agent's invariants are preserved as table CHECKs: `session_id`/`prompt_version`
relax to nullable for session-less integration/pipeline rows, but a CHECK forces
both NOT NULL whenever `kind='agent'`, so an agent run can never be written without
them. The owner-only RLS posture (is_owner()) carries forward under the new name.

NOT created here: the dispatcher, the E1 scope carrier, or the Integrator's run
persistence — those are sibling Wave-1 tasks A2/A3/A4. This migration only renames
and extends the tables.

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-15
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- rename in place: dependent FKs follow the table automatically --------
    # agent_episodes / agent_turns reference agent_runs(id); a
    # RENAME rewrites those constraints to the new name, so they keep working
    # untouched (verified by the agent regression tests still passing).
    op.execute("ALTER TABLE app.agent_runs RENAME TO runs")
    op.execute("ALTER TABLE app.agent_steps RENAME TO run_steps")

    # --- relax the agent-specific NOT NULLs; gate them behind kind='agent' ----
    op.execute("ALTER TABLE app.runs ALTER COLUMN session_id DROP NOT NULL")
    op.execute("ALTER TABLE app.runs ALTER COLUMN prompt_version DROP NOT NULL")

    # --- the kind discriminator + the columns the engine needs ----------------
    # kind defaults to 'agent' so the rename backfills every existing row as an
    # agent run. ran_as records E1's scope choice (scoped vs owner-system) so the
    # audit shows owner-system, not a smuggled escalation; domain_code/principal_id
    # carry the trigger's fail-closed stamp + identity (filled by A3's carrier).
    op.execute(
        """
        ALTER TABLE app.runs
            ADD COLUMN kind text NOT NULL DEFAULT 'agent'
                CHECK (kind IN ('agent', 'integration', 'pipeline')),
            ADD COLUMN pipeline text,
            ADD COLUMN trigger_id uuid REFERENCES app.triggers(id) ON DELETE SET NULL,
            ADD COLUMN ran_as text NOT NULL DEFAULT 'scoped'
                CHECK (ran_as IN ('scoped', 'system')),
            ADD COLUMN domain_code text REFERENCES app.domains(code),
            ADD COLUMN principal_id uuid REFERENCES app.principals(id)
        """
    )

    # The agent invariant survives the nullable relaxation: an agent run still
    # requires both session_id and prompt_version.
    op.execute(
        """
        ALTER TABLE app.runs
            ADD CONSTRAINT runs_agent_requires_session CHECK (
                kind <> 'agent'
                OR (session_id IS NOT NULL AND prompt_version IS NOT NULL)
            )
        """
    )

    # --- run_steps.job_id: nullable FK to the executor, SET NULL on job age-out -
    # A run step may reference the app.jobs row it enqueued; ON DELETE SET NULL so
    # a job aging out never breaks a run-log read (N2). app.jobs stays the executor
    # — runs is the audit layer above it (§7 "runs vs jobs").
    op.execute(
        "ALTER TABLE app.run_steps"
        " ADD COLUMN job_id uuid REFERENCES app.jobs(id) ON DELETE SET NULL"
    )

    # --- RLS: the policy + grants follow the rename; rename the policies to match -
    # ENABLE/FORCE state and the policy bodies survive the table rename; only the
    # policy names are stale. Renaming keeps the owner-only posture explicit. The
    # agent posture (owner-only is_owner() for kind='agent' rows) is unchanged —
    # is_owner() already gates the whole table, so no non-owner ever sees any run.
    op.execute("ALTER POLICY agent_runs_owner ON app.runs RENAME TO runs_owner")
    op.execute("ALTER POLICY agent_steps_owner ON app.run_steps RENAME TO run_steps_owner")


def downgrade() -> None:
    op.execute("ALTER TABLE app.run_steps DROP COLUMN job_id")
    op.execute("ALTER TABLE app.runs DROP CONSTRAINT runs_agent_requires_session")
    op.execute(
        """
        ALTER TABLE app.runs
            DROP COLUMN principal_id,
            DROP COLUMN domain_code,
            DROP COLUMN ran_as,
            DROP COLUMN trigger_id,
            DROP COLUMN pipeline,
            DROP COLUMN kind
        """
    )
    # Restore the original NOT NULLs (safe: only agent rows exist pre-engine).
    op.execute("ALTER TABLE app.runs ALTER COLUMN prompt_version SET NOT NULL")
    op.execute("ALTER TABLE app.runs ALTER COLUMN session_id SET NOT NULL")

    op.execute("ALTER POLICY run_steps_owner ON app.run_steps RENAME TO agent_steps_owner")
    op.execute("ALTER POLICY runs_owner ON app.runs RENAME TO agent_runs_owner")

    op.execute("ALTER TABLE app.run_steps RENAME TO agent_steps")
    op.execute("ALTER TABLE app.runs RENAME TO agent_runs")
