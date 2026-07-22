"""Checkpoint state for a background deepest-research run (docs/plans/
DEEPEST_RESEARCH_TOOL_PLAN.md, R5).

A deepest run is a detached, minutes-to-hours background task (R3) — so it must survive a
worker/box restart mid-run. This table is its durable checkpoint: after each committed
round the run writes its accumulated findings/sources/coverage (`state`), the last
committed round, and the committed spend/agent counters, so a restart rehydrates and
CONTINUES from the last committed round rather than re-running the whole thing or losing
it. `wall_clock_deadline` is stored as an ABSOLUTE UTC instant (not a monotonic clock,
which is meaningless across a restart), so a resumed run re-derives its remaining
wall-clock. `resumed_at` is the atomic one-shot resume claim (mirrors 0138's
media-analysis pattern), so exactly one restarted process picks up a given run.

RLS `external` domain, like `app.research_reports` (0140): the run's research is external
web data, and a scoped non-owner principal can neither read nor write it (CLAUDE.md rule 3).
"""

from alembic import op

revision = "0147"
down_revision = "0146"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.research_run_state (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- the lane's run identifier (the DeepestRunLane key); one row per run.
            run_id text NOT NULL,
            -- the owner chat session the run reports progress into (R6) and was kicked from.
            session_id uuid,
            question text NOT NULL,
            status text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'done', 'failed', 'cancelled')),
            -- the last COMMITTED round; a restart continues from here (uncommitted in-flight
            -- round work is re-run, not reconstructed).
            round integer NOT NULL DEFAULT 0,
            -- the owner-set token ceiling (the hard terminal bound) and the absolute-UTC
            -- wall-clock deadline; a resumed run re-derives its remaining monotonic clock.
            ceiling_tokens bigint NOT NULL DEFAULT 0,
            wall_clock_deadline timestamptz,
            -- committed at each round boundary, so a resumed run rewinds its tree counters
            -- to the last committed values (never re-spends / double-counts the re-run round).
            spent_tokens bigint NOT NULL DEFAULT 0,
            agents_spawned integer NOT NULL DEFAULT 0,
            -- the rehydrate payload: accumulated findings, the citation registry, coverage.
            state jsonb NOT NULL DEFAULT '{}'::jsonb,
            -- the atomic one-shot resume claim (0138 pattern): NULL = unclaimed; the claim
            -- UPDATE sets it once (WHERE resumed_at IS NULL), so exactly one process resumes.
            resumed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            domain_code text NOT NULL DEFAULT 'external' REFERENCES app.domains(code),
            UNIQUE (run_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX research_run_state_status_idx ON app.research_run_state (status, updated_at)"
    )

    op.execute("ALTER TABLE app.research_run_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.research_run_state FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY research_run_state_domain ON app.research_run_state
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.research_run_state TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.research_run_state")
