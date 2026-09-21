"""`endpoint_settings.debug_overlay` — the mic meter becomes a debug tool (§10.4bi).

The owner: *"go ahead and turn the audio meter on the left side to only be rendered if we
enable a debug mode. It's not needed all the time."*

The meter earned its place. `display.c`'s header argues it well: a microphone has no symptom,
silence could be the ADC, the PGA, the I2S receive direction or a pin map read from the wrong
end of the link, and none of those announce themselves — so a meter that is simply always
running answers it at a glance. It is how the first successful decode was confirmed to be
hearing a real voice rather than a number the firmware made up.

That was a bring-up argument and bring-up is over. What it buys now is a green bar down the
left edge of a pet in a four-year-old's bedroom, permanently, so that the toy is always
slightly an instrument. The right end state for a diagnostic is not deletion — it is a
switch — because the next time the microphone goes quiet the meter is the fastest answer in
the building.

**A column here rather than a firmware constant** for the reason §10.4ab gave for the other
three: the owner has no terminal (`CLAUDE.md` #10), so anything only a rebuild can change is
something they cannot change. This one is reachable from the PWA and from the debug API.

**Boolean, defaulting to false.** The panels in the house are a product now, and a debug
overlay that defaults on is a debug overlay nobody turns off.

The existing policies cover it: the owner may write, a `device_key` may only SELECT, and a
column inherits both. A panel that could switch on its own debug overlay would be harmless,
but it also has no reason to.

Revision ID: 0207
Revises: 0206
Create Date: 2026-09-21
"""

from alembic import op

revision = "0207"
down_revision = "0206"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.endpoint_settings ADD COLUMN debug_overlay boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.endpoint_settings DROP COLUMN debug_overlay")
