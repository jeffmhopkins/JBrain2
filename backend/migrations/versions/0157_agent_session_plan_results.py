"""`agent_session_plans.results` — the per-plan step-results scratchpad
(docs/plans/JERV_PLANNING_TOOL_PLAN.md).

A long owner-approved plan is executed across turns, but each step's findings used to live
only in that turn's collapsed tool trace — invisible in the chat and unavailable to later
steps as a clean synthesis. This adds an APPEND-ONLY, index-ordered scratchpad: a JSONB array
of `{heading?, note}` entries. `write_plan_result` appends one entry per call (never
overwriting an earlier index, so no turn can erase a prior step's results from view), and
`read_plan` returns the whole scratchpad — so every later step, and the final write-up, reads
all prior results in one place. It rides the existing owner-only row, so the FORCE RLS on
`agent_session_plans` already governs it (no new table, no new policy).

Revision ID: 0157
Revises: 0156
Create Date: 2026-08-07
"""

from alembic import op

revision = "0157"
down_revision = "0156"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An append-only, index-ordered scratchpad: a JSONB array of `{heading?, note}` entries,
    # one per `write_plan_result` call. Ordered by append (the array index), never rewritten
    # in place — so a later step can add results but can never erase an earlier step's.
    op.execute(
        "ALTER TABLE app.agent_session_plans ADD COLUMN results jsonb NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.agent_session_plans DROP COLUMN IF EXISTS results")
