"""GrowBot persona faces: the ember, a single glowing lens.

docs/plans/GROWBOT_PLAN.md §2a′. The owner asked (2026-11-12) for a face for his adult
persona, Dave: one red lens in a steel bezel whose light is its voice
(docs/mocks/growbot-face/d-ember.html). growbot-face.js draws it; migration 0259's CHECK is
widened to let a persona wear it. Lantern stays the default.

No new table, so no new isolation test; the column keeps `growbot_personas`' owner-only RLS.

Revision ID: 0264
Revises: 0263
Create Date: 2026-11-12
"""

from alembic import op

revision = "0264"
down_revision = "0263"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.growbot_personas DROP CONSTRAINT growbot_personas_face_check")
    op.execute(
        "ALTER TABLE app.growbot_personas ADD CONSTRAINT growbot_personas_face_check"
        " CHECK (face IN ('lantern', 'ostrich', 'robot', 'ember', 'pixel'))"
    )


def downgrade() -> None:
    op.execute("UPDATE app.growbot_personas SET face = 'lantern' WHERE face = 'ember'")
    op.execute("ALTER TABLE app.growbot_personas DROP CONSTRAINT growbot_personas_face_check")
    op.execute(
        "ALTER TABLE app.growbot_personas ADD CONSTRAINT growbot_personas_face_check"
        " CHECK (face IN ('lantern', 'ostrich', 'robot', 'pixel'))"
    )
