"""Extend `view_audit.path` with 'roster' — the map's presence pins are a location read.

The member roster now carries each visible subject's latest coordinate (the map pin),
so loading the roster is a who-saw-whom location view, like the trail read ('history')
and the live socket ('live'). Audit it under a distinct 'roster' path so the dashboard
pins are observable in the who-saw-whom log, per the 0069 invariant.

Revision ID: 0076
Revises: 0075
Create Date: 2026-06-20
"""

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.view_audit DROP CONSTRAINT view_audit_path_check")
    op.execute(
        "ALTER TABLE app.view_audit ADD CONSTRAINT view_audit_path_check"
        " CHECK (path IN ('history', 'live', 'poke', 'roster'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM app.view_audit WHERE path = 'roster'")
    op.execute("ALTER TABLE app.view_audit DROP CONSTRAINT view_audit_path_check")
    op.execute(
        "ALTER TABLE app.view_audit ADD CONSTRAINT view_audit_path_check"
        " CHECK (path IN ('history', 'live', 'poke'))"
    )
