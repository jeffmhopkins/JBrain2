"""GrowBot persona settings: the creature's own localStorage keys, kept on the box.

docs/plans/GROWBOT_PLAN.md §2c. The soul, visual memories, face and calibration already lived
on the box, but upstream's page kept the rest of what a creature chose or was told to remember
(its coat colour, its games guide, its volume, which camera it looks through, remembered
`?flags`) in the phone's localStorage, so a new or wiped phone came up as a stranger. The owner
asked (2026-11-12) for the phone to be only a display: `prefs` holds those keys per persona,
`growbot-box.js` loads them before the page runs and sends each change back.

- `prefs` — key to string; NULL until a phone first sends its own, so the first phone a persona
  opens on keeps what it already had.
- `prefs_version` — bumped by every change, so a running phone notices an owner's edit on its
  poll and applies it where it stands.

The columns ride `growbot_personas`' owner-only RLS; no new table, so no new isolation test.

Revision ID: 0263
Revises: 0262
Create Date: 2026-11-12
"""

from alembic import op

revision = "0263"
down_revision = "0262"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.growbot_personas ADD COLUMN prefs jsonb"
        " CHECK (prefs IS NULL OR jsonb_typeof(prefs) = 'object')"
    )
    op.execute(
        "ALTER TABLE app.growbot_personas ADD COLUMN prefs_version integer NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.growbot_personas DROP COLUMN prefs_version")
    op.execute("ALTER TABLE app.growbot_personas DROP COLUMN prefs")
