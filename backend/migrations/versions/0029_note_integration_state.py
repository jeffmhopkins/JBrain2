"""Add notes.integration_state — the note→graph Integrator lifecycle.

Tracks whether a note has been through the new Integrator agent
(docs/INTEGRATOR_PLAN.md §4): an indexed note is `pending_integration` until
the agent produces its IntegrationIntent and the arbiter commits, then
`integrated`. ADDITIVE for now — the existing analyze_note path still runs and
nothing yet consumes this column; the trigger cutover (W3.3) is deferred so the
branch stays coherent. The column lives on `app.notes`, already RLS-scoped by
domain_code, so it introduces no new firewall surface.

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-13
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

_STATES = ("pending_integration", "integrating", "integrated", "stale", "skipped")


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.notes ADD COLUMN integration_state text NOT NULL "
        "DEFAULT 'pending_integration'"
    )
    op.execute(
        "ALTER TABLE app.notes ADD CONSTRAINT notes_integration_state_check "
        f"CHECK (integration_state IN ({', '.join(repr(s) for s in _STATES)}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.notes DROP CONSTRAINT notes_integration_state_check")
    op.execute("ALTER TABLE app.notes DROP COLUMN integration_state")
