"""Reap prior runs of a pipeline when it fires again, and clean the orphan backlog.

A preconditioned hourly sweep (inbox triage) whose local model is often not resident
keeps deferring, and the pre-reap scheduler left a trail behind it: every fire opened a
fresh `running` run, and a coalesced/deferred one never finalized (finalize is per job
step), so the `app.runs` table filled with orphaned `running` rows that render as `—`
on the Ops "Runs" surface. The engine now supersedes a pipeline's prior running run
when it fires again ('latest run wins', workflow.runlog.supersede_running_runs), which
needs two new terminal states:

- `app.runs.status` gains `superseded` — a run ended because a newer run of the same
  pipeline took over (neither `done` nor an `error`).
- `app.jobs.status` gains `canceled` — a deferred sweep's queued job dropped so a fresh
  job can take its place (not a `failed` attempt).

Then one-shot cleans the backlog the old scheduler left: cancel each still-queued job of
a running pipeline run and supersede those runs. Safe at deploy — the worker container
is recreated, so nothing is genuinely in flight, and every affected sweep re-fires on
its own schedule. A run with an actively-`running` job is left alone (its job finishes
normally), mirroring the runtime guard.

Revision ID: 0128
Revises: 0127
Create Date: 2026-07-06
"""

from alembic import op

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None

# The runs status CHECK keeps its legacy name (`agent_runs_status_check`) from when the
# table was agent-only; the pipeline/subagent kinds were added later without renaming it.
_RUNS_CHECK = "agent_runs_status_check"
_JOBS_CHECK = "jobs_status_check"


def upgrade() -> None:
    op.execute(f"ALTER TABLE app.runs DROP CONSTRAINT {_RUNS_CHECK}")
    op.execute(
        f"ALTER TABLE app.runs ADD CONSTRAINT {_RUNS_CHECK}"
        " CHECK (status IN ('running', 'done', 'error', 'superseded'))"
    )
    op.execute(f"ALTER TABLE app.jobs DROP CONSTRAINT {_JOBS_CHECK}")
    op.execute(
        f"ALTER TABLE app.jobs ADD CONSTRAINT {_JOBS_CHECK}"
        " CHECK (status IN ('queued', 'running', 'done', 'failed', 'canceled'))"
    )

    # One-shot backlog reap (mirrors supersede_running_runs). Cancel the still-queued
    # jobs of running pipeline runs that have no actively-running step, then supersede
    # those runs. The NOT EXISTS guard leaves a genuinely in-flight run to finish.
    op.execute(
        """
        UPDATE app.jobs j
           SET status = 'canceled', finished_at = now(),
               last_error = 'superseded: newer run of the same pipeline (0128 backfill)'
          FROM app.run_steps s
          JOIN app.runs r ON r.id = s.run_id
         WHERE s.job_id = j.id
           AND r.kind = 'pipeline' AND r.status = 'running'
           AND j.status = 'queued'
           AND NOT EXISTS (
               SELECT 1 FROM app.run_steps s2 JOIN app.jobs j2 ON j2.id = s2.job_id
               WHERE s2.run_id = r.id AND j2.status = 'running'
           )
        """
    )
    op.execute(
        """
        UPDATE app.runs r
           SET status = 'superseded', ended_at = now(), progress_note = NULL
         WHERE r.kind = 'pipeline' AND r.status = 'running'
           AND NOT EXISTS (
               SELECT 1 FROM app.run_steps s JOIN app.jobs j ON j.id = s.job_id
               WHERE s.run_id = r.id AND j.status = 'running'
           )
        """
    )


def downgrade() -> None:
    # Fold the new terminal states onto the nearest legacy status so the narrowed
    # CHECKs hold again, then restore them.
    op.execute("UPDATE app.runs SET status = 'done' WHERE status = 'superseded'")
    op.execute("UPDATE app.jobs SET status = 'failed' WHERE status = 'canceled'")
    op.execute(f"ALTER TABLE app.runs DROP CONSTRAINT {_RUNS_CHECK}")
    op.execute(
        f"ALTER TABLE app.runs ADD CONSTRAINT {_RUNS_CHECK}"
        " CHECK (status IN ('running', 'done', 'error'))"
    )
    op.execute(f"ALTER TABLE app.jobs DROP CONSTRAINT {_JOBS_CHECK}")
    op.execute(
        f"ALTER TABLE app.jobs ADD CONSTRAINT {_JOBS_CHECK}"
        " CHECK (status IN ('queued', 'running', 'done', 'failed'))"
    )
