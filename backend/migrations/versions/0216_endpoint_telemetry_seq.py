"""`endpoint_settings.telemetry_seq` — ask every panel to report NOW.

The decode ring is the only window onto what a panel actually hears, and it rides the
telemetry post, which is on `CHECK_PERIOD_MS` — fifteen minutes. That interval is not an
oversight and must not be shortened: `ROOM_ENDPOINT_PLAN.md` §10.4bh reads a report at 6-7 s
of uptime as PROOF OF A BOOT, a diagnostic that only works while the interval is long.

So the measurement loop for "did she say it, and what did the decoder make of it?" was: say
the phrase, then wait up to a quarter of an hour to find out. There is no way to shorten it
from here. Rebooting the panel DOES force a report within seconds — and wipes `s_decode`,
which is the very thing being asked for, so the fast path returns an empty ring.

A COUNTER, NOT A TIMESTAMP, and not a boolean. A boolean has to be cleared, which means the
box guessing when every panel has seen it; two panels poll independently and a flag consumed
by the first is a flag the second never sees. A timestamp collides when raised twice inside
its resolution and ties the panel to the box's clock. A counter the panel simply remembers:
it adopts whatever it sees on its first poll after boot, and posts when the number CHANGES.
Exactly once per raise, per panel, with no state on the box to expire and nothing to clear.

GLOBAL rather than per-panel, like the other five knobs here, because the question it answers
is about the house: both panels report, and a measurement wants both sides of the room.

Revision ID: 0216
Revises: 0215
Create Date: 2026-09-26
"""

from alembic import op

revision = "0216"
down_revision = "0215"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.endpoint_settings ADD COLUMN telemetry_seq bigint NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.endpoint_settings DROP COLUMN telemetry_seq")
