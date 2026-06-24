"""Per-step execution log on run_steps (the Runs "full logs" review trace).

A run step records that a job ran (ok + cost_tokens); `detail` adds the job's
captured structured-log trace — the step-by-step actions/reasoning (LLM calls,
build/integration events) — so an owner can review WHAT a run did, not just that
it finished. JSONB array of compact event dicts, owner-only like the rest of the
run log (runs/run_steps RLS). Nullable: pre-existing steps and any job that
emitted nothing carry NULL, not an empty array.

Revision ID: 0089
Revises: 0088
Create Date: 2026-06-24
"""

from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.run_steps ADD COLUMN detail jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE app.run_steps DROP COLUMN detail")
