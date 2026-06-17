"""Add the `predicate-canon` proposal kind (Loop 3a, Wave 2).

The agent-proposed predicate review (`predicate_review`) stages a `predicate-canon` Proposal whose
leaves each apply an owner-approved `new_predicate` card resolution. Extends the closed `kind` CHECK
the way 0027 added `appointment`. Mirrors how `skill-promotion` already lives in the set.

Revision ID: 0057
Revises: 0056
Create Date: 2026-06-17
"""

from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None

_OLD = (
    "('correction', 'knowledge', 'appointment', 'wiki-restructure',"
    " 'prompt-edit', 'skill-promotion', 'egress')"
)
_NEW = (
    "('correction', 'knowledge', 'appointment', 'wiki-restructure',"
    " 'prompt-edit', 'skill-promotion', 'predicate-canon', 'egress')"
)


def _set_kind_check(values: str) -> None:
    op.execute("ALTER TABLE app.proposals DROP CONSTRAINT proposals_kind_check")
    op.execute(
        f"ALTER TABLE app.proposals ADD CONSTRAINT proposals_kind_check CHECK (kind IN {values})"
    )


def upgrade() -> None:
    _set_kind_check(_NEW)


def downgrade() -> None:
    _set_kind_check(_OLD)
