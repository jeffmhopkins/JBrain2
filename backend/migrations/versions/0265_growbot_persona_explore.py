"""GrowBot persona exploring: whether a persona's creature uses quiet time to explore alone.

docs/plans/GROWBOT_PLAN.md §2c. Upstream's 11-11 quiet-beat text hands a creature left alone
the time to reach for its wants with its body; the old text kept it waiting as company. The
owner asked (2026-11-12) for that to be a persona setting, on by default, rather than a phone
URL switch: the phone reads it with the soul and its poll carries a change, like the face
(0259) and the voice (0264). Existing personas get it on too.

The column rides `growbot_personas`' owner-only RLS; no new table, so no new isolation test.

Revision ID: 0265
Revises: 0264
Create Date: 2026-11-12
"""

from alembic import op

revision = "0265"
down_revision = "0264"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.growbot_personas ADD COLUMN explore boolean NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE app.growbot_personas DROP COLUMN explore")
