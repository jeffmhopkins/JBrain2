"""`agent_session_plans` — multiple independent plans per chat
(docs/plans/JERV_PLANNING_TOOL_PLAN.md).

The plan row was primary-keyed by `session_id` — one plan per conversation — so a
second plan drafted in the same chat overwrote the first, carrying its stale results
scratchpad and stranding the omnibox on mixed state. This re-keys the table on a new
`plan_id` so a conversation can hold MANY plans, each with its own body, status, and
results: an old plan's inline card keeps its own history, and the "active" plan (the
latest-created) is what the continuation loop, the tools, and the omnibox track.

`session_id` stays a NOT-NULL cascade FK (now non-unique — many plans per session); a
`(session_id, created_at)` index backs the active-plan lookup. RLS/grants are unchanged
(the owner-only FORCE policy rides the same table).

Revision ID: 0158
Revises: 0157
Create Date: 2026-08-07
"""

from alembic import op

revision = "0158"
down_revision = "0157"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh per-plan identity. Existing rows each get one uuid — one plan per session,
    # preserved as that session's (sole, hence active) plan.
    op.execute(
        "ALTER TABLE app.agent_session_plans"
        " ADD COLUMN plan_id uuid NOT NULL DEFAULT gen_random_uuid()"
    )
    # Re-key: session_id was the PK (unique); drop it so a session can hold many plans, and
    # make plan_id the PK. session_id stays NOT NULL with its cascade FK, just no longer unique.
    op.execute("ALTER TABLE app.agent_session_plans DROP CONSTRAINT agent_session_plans_pkey")
    op.execute("ALTER TABLE app.agent_session_plans ADD PRIMARY KEY (plan_id)")
    # The active plan is the latest-created for a session — this index backs that ORDER BY
    # and every session-scoped lookup (re-injection, the tools, the omnibox resolve).
    op.execute(
        "CREATE INDEX agent_session_plans_session_created_idx"
        " ON app.agent_session_plans (session_id, created_at DESC)"
    )


def downgrade() -> None:
    # Reverting to one-plan-per-session: keep each session's latest plan, drop the rest, then
    # restore session_id as the PK. (A destructive collapse — later plans are lost.)
    op.execute(
        """
        DELETE FROM app.agent_session_plans a
        USING app.agent_session_plans b
        WHERE a.session_id = b.session_id
          AND (a.created_at, a.plan_id) < (b.created_at, b.plan_id)
        """
    )
    op.execute("DROP INDEX IF EXISTS app.agent_session_plans_session_created_idx")
    op.execute("ALTER TABLE app.agent_session_plans DROP CONSTRAINT agent_session_plans_pkey")
    op.execute("ALTER TABLE app.agent_session_plans ADD PRIMARY KEY (session_id)")
    op.execute("ALTER TABLE app.agent_session_plans DROP COLUMN IF EXISTS plan_id")
