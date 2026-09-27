"""`endpoint_settings.volume` — default 95, and off the old ceiling.

The owner: *"his volume is not changing when I change it in the panel control of the pwa ... I
want to default to 95 ... the speaker just is not loud enough"*, then, correcting himself,
*"the volume slider will only go to 85."*

**The slider was not broken; it was on its stop.** `VOLUME_MAX` was 85 and the stored value was
already 85, so every further increase clamped back to where it started and the control looked
dead. The ceiling is 100 now (see `endpoint.py`), and this moves the stored value to the 95 that
was asked for.

ONLY ROWS SITTING ON THE OLD CEILING. A migration that overwrote a volume somebody had chosen
would be this same bug from the other direction — a setting changing under the owner without
being asked. 85 is the one value that cannot have been chosen deliberately in preference to
something higher, because nothing higher was reachable.

It does not by itself make a panel louder: measured the same day, the box's volume had never
reached Lydian's panel at all (`levels` empty at 941 s of uptime), which is a separate fault
instrumented in the same release.

Revision ID: 0217
Revises: 0216
Create Date: 2026-09-27
"""

from alembic import op

revision = "0217"
down_revision = "0216"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.endpoint_settings ALTER COLUMN volume SET DEFAULT 95")
    op.execute("UPDATE app.endpoint_settings SET volume = 95 WHERE volume = 85")


def downgrade() -> None:
    op.execute("ALTER TABLE app.endpoint_settings ALTER COLUMN volume SET DEFAULT 70")
