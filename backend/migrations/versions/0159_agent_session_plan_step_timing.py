"""`agent_session_plans.step_started_at` — per-step timing for the plan card
(docs/plans/JERV_PLANNING_TOOL_PLAN.md).

A plan's steps are executed one-per-turn across continuations, but nothing recorded how
long each step took, so the plan card / omnibox modal couldn't show the owner the time
spent on each step (a candidate-report step is a multi-minute deep-research run). This adds
a nullable `step_started_at`: the continuation stamps it when a step's turn begins, and
`write_plan_result` (complete_step) reads it to record the step's elapsed `secs` onto that
step's results entry, then clears it. It rides the existing owner-only row, so the FORCE RLS
on `agent_session_plans` already governs it (no new table, no new policy).

Revision ID: 0159
Revises: 0158
Create Date: 2026-08-08
"""

from alembic import op

revision = "0159"
down_revision = "0158"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: a plan not mid-step has no start stamp, and the column is only ever set while a
    # step's turn is in flight (cleared when the step's result is recorded).
    op.execute("ALTER TABLE app.agent_session_plans ADD COLUMN step_started_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE app.agent_session_plans DROP COLUMN IF EXISTS step_started_at")
