"""GrowBot persona faces: which face a persona's creature wears.

docs/plans/GROWBOT_PLAN.md §2c. The sidecar's growbot-face.js draws one of four faces over
upstream's sky; until now a phone chose by `?face=` or its own localStorage, so a persona
looked like whatever the last one on that phone did. Here it is the persona's: the owner
picks it in JBrain, the phone reads it with the soul and switches when a poll says it changed.
Lantern is the default (owner, 2026-11-11), the existing personas included.

The column rides `growbot_personas`' owner-only RLS; no new table, so no new isolation test.

Revision ID: 0259
Revises: 0258
Create Date: 2026-11-11
"""

from alembic import op

revision = "0259"
down_revision = "0258"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.growbot_personas ADD COLUMN face text NOT NULL DEFAULT 'lantern'"
        " CHECK (face IN ('lantern', 'ostrich', 'robot', 'pixel'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.growbot_personas DROP COLUMN face")
