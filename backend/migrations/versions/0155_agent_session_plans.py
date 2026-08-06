"""`agent_session_plans` — jerv's per-conversation plan (docs/plans/JERV_PLANNING_TOOL_PLAN.md).

A single plan document per chat: full free-text body plus a lifecycle `status`
(`not_approved | approved | in_work`). jerv reads/writes the body and may set
`not_approved`/`in_work`; ONLY the owner flips it to `approved` (the api approve
endpoint), so web content jerv reads can never talk it into self-approving its own
plan. One row per `agent_sessions.id`, cascade-deleted with the chat — modeled on
`turn_tool_artifacts` (0151): a per-session row, but the FIREWALL here is owner-only
(`app.is_owner()`, like `archivist_memory` 0094), not a domain scope — a plan is
conversation-local, not knowledge-base data, and jerv is empty-scoped.

Revision ID: 0155
Revises: 0154
Create Date: 2026-08-06
"""

from alembic import op

revision = "0155"
down_revision = "0154"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.agent_session_plans (
            session_id uuid PRIMARY KEY
                REFERENCES app.agent_sessions(id) ON DELETE CASCADE,
            -- The full plan text (Markdown), authored and rewritten by jerv.
            body text NOT NULL DEFAULT '',
            -- The lifecycle status. not_approved: a draft awaiting the owner. approved:
            -- the owner signed off (owner-only transition). in_work: jerv is executing
            -- an approved plan (jerv sets this once approved).
            status text NOT NULL DEFAULT 'not_approved'
                CHECK (status IN ('not_approved', 'approved', 'in_work')),
            -- Auto-continuation bookkeeping (docs/plans/JERV_PLANNING_TOOL_PLAN.md). A long
            -- approved plan is executed across turns: when a turn ends mid-plan, a
            -- continuation is due at `continuation_due_at` (NULL = none pending); the
            -- in-process sweep claims it, runs one headless step, and re-arms. `awaiting_owner`
            -- is jerv's explicit "I need you — do NOT auto-continue" opt-out. `continuations_used`
            -- caps the chain so it can never run unbounded; the owner sending any message
            -- clears the due-time, the flag, and the counter (a human-anchored reset).
            continuation_due_at timestamptz,
            awaiting_owner boolean NOT NULL DEFAULT false,
            continuations_used integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # The sweep claims due continuations by (due_at, status) — a partial index keeps that
    # claim cheap and only covers rows that actually have one pending.
    op.execute(
        "CREATE INDEX agent_session_plans_due_idx ON app.agent_session_plans"
        " (continuation_due_at) WHERE continuation_due_at IS NOT NULL"
    )
    op.execute("ALTER TABLE app.agent_session_plans ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.agent_session_plans FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY agent_session_plans_owner ON app.agent_session_plans
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.agent_session_plans TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.agent_session_plans")
