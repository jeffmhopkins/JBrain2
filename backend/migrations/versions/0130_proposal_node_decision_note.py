"""Record the owner's reason when a proposal node is declined.

Inline approvals (docs/archive/INLINE_APPROVALS_PLAN.md §3.3): declining a staged
operation inline can carry a free-text reason so the assistant learns *why*, not just
that it was declined. The reason is owner-eyes feedback folded into the enact outcome
the agent sees; it is not a graph fact and never leaves the owner's RLS scope (the
column inherits `app.proposal_nodes`' owner-only, domain-narrowed RLS from migration
0018 — no policy change).

Additive and nullable: existing nodes and the un-reasoned decline path are unaffected.

Revision ID: 0130
Revises: 0129
Create Date: 2026-07-13
"""

from alembic import op

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.proposal_nodes ADD COLUMN decision_note text")


def downgrade() -> None:
    op.execute("ALTER TABLE app.proposal_nodes DROP COLUMN decision_note")
