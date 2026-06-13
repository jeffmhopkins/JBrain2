"""Add the `appointment` proposal kind (docs/ROADMAP.md Phase 4).

The agent has no privileged write into the appointments projection (it is derived
from notes, the sole sources of truth, #7). Creating, rescheduling, or cancelling
an appointment therefore STAGES a Proposal — a new `appointment` kind — whose
approved leaf re-enters as an agent note through the normal pipeline, exactly like
a correction. This widens the kind CHECK to admit it.

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-13
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_OLD = "('correction', 'knowledge', 'wiki-restructure', 'prompt-edit', 'skill-promotion', 'egress')"
_NEW = (
    "('correction', 'knowledge', 'appointment', 'wiki-restructure',"
    " 'prompt-edit', 'skill-promotion', 'egress')"
)


def _set_kind_check(values: str) -> None:
    op.execute("ALTER TABLE app.proposals DROP CONSTRAINT proposals_kind_check")
    op.execute(f"ALTER TABLE app.proposals ADD CONSTRAINT proposals_kind_check CHECK (kind IN {values})")


def upgrade() -> None:
    _set_kind_check(_NEW)


def downgrade() -> None:
    _set_kind_check(_OLD)
