"""`endpoint_settings.brightness` — off the floor a dead control left it on.

**Brightness has never worked.** The panel is driven over QSPI, where a command is a 32-bit
prologue (CO5300 datasheet V0.01 p.21: instruction `02h`, then `{8'h00, CMD[7:0], 8'h00}`), and
`display.c` sent a bare `0x51`. That goes out as instruction `0x00`, which is not a defined QSPI
instruction; the controller discarded it and the SPI layer returned `ESP_OK` regardless. Only the
init array — which goes through the vendor driver's own wrapper — ever landed, so every panel has
sat at the `0xFF` it was given at boot since August.

THE SLIDER WAS THEREFORE A DEAD CONTROL, AND IT IS SITTING ON ITS FLOOR. The stored value is 10,
which is `BRIGHTNESS_MIN` — what you get by dragging a slider to the end to see whether it does
anything. It did not. The moment the framing is fixed that 10 becomes real, and 10 of 255 in a
bedroom is the panel that "looks broken" which `BRIGHTNESS_MIN`'s own comment exists to prevent.

So this puts it back to 255 — NOT a new preference, but the brightness the rooms have actually
been running at all along. The fix then changes nothing anyone can see, and the first deliberate
dim after it will be the first one that has ever worked.

ONLY THE FLOOR, for the reason migration 0217 only moved volumes sitting on the old ceiling: 10
is the one value that cannot have been chosen in preference to something dimmer.

Revision ID: 0218
Revises: 0217
Create Date: 2026-09-27
"""

from alembic import op

revision = "0218"
down_revision = "0217"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE app.endpoint_settings SET brightness = 255 WHERE brightness = 10")


def downgrade() -> None:
    """Deliberately not reversible: putting a panel back on a floor that only looked chosen
    because nothing could act on it would be re-creating the bug's only visible consequence."""
