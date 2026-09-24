"""`endpoint_settings.mic_agc` — the codec's own automatic gain control, as a knob.

The owner, after the first real voice message came off a panel: *"we need the auto gain control
from panel mic too, it was way too quiet."*

**The ES8311 has an ALC and it has never been on.** `es8311.c` writes REG1B and REG1C (automute,
HPF) and never touches REG18, the register that enables it, so ALC has sat at the chip's reset
default since first bring-up. `audio.c` reads it and reports `00 already-off` in every telemetry
report this box has ever received.

It was read in the first place because of a DIFFERENT complaint — *"when it beeps it kind of
rails the audio gain meter for 4 to 5 seconds"* — and auto-gain was the suspect. The reading
cleared it: the ALC was already off, so it cannot have caused that ramp. The other candidate
named in the same comment is still standing (`REG44 = 0x58` routes the DAC into the ADC, so the
panel hears its own speaker). Which matters here, because the reason auto-gain was left alone
turns out to be a suspicion that the measurement disproved.

**A fixed PGA cannot serve these two panels.** Both run `mic_gain_db = 30`; the last reports
before they were unplugged read `mic_peak` 32767 on one — full scale, clipping — and 814 on the
other, 2.5% of it. That is the case for gain that adapts rather than a better constant.

**A SETTING, NOT A REBUILD.** The owner has no terminal (CLAUDE.md #10) and this is a knob that
wants trying against a real room rather than deciding in a commit — the last audio number that
"only needed one value" took two rounds. Default OFF: it preserves exactly today's behaviour, so
the upgrade changes nothing until somebody asks it to.

Revision ID: 0214
Revises: 0213
Create Date: 2026-09-24
"""

from alembic import op

revision = "0214"
down_revision = "0213"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.endpoint_settings ADD COLUMN mic_agc boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.endpoint_settings DROP COLUMN mic_agc")
