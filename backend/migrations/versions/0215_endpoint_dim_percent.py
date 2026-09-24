"""`endpoint_settings.dim_percent` — how dim "dim" actually is.

The owner, the morning after the sleep pose shipped: *"the bird was sleeping when I saw it this
morning I think, but the screen wasn't dimmed."*

**It was dimmed, and that is the point.** `screen_level()` returns `configured >> 2` at the dim
stage — a quarter of the register value — and with brightness at 255 that is 63. The stage
machinery worked exactly as written; 63/255 simply does not READ as dim in a bedroom, because a
quarter of a register is nowhere near a quarter of perceived brightness. Vision is roughly
logarithmic and the panel's response is not linear either.

So the shift was a guess dressed as arithmetic, in the same family as the movement threshold that
`screen.h` admits "has never met a bedside table". Replacing one guess with another guess would
be the same mistake with a different constant, and the owner cannot reach a constant: he has no
terminal (CLAUDE.md #10), and the last screen number that "only needed one value" was wrong twice.

**Percent rather than a shift**, because a shift can only halve. 25 reproduces `>> 2` exactly, so
this upgrade changes nothing at all until somebody moves the slider — and a bedroom probably
wants something nearer 10.

Revision ID: 0215
Revises: 0214
Create Date: 2026-09-24
"""

from alembic import op

revision = "0215"
down_revision = "0214"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.endpoint_settings"
        " ADD COLUMN dim_percent smallint NOT NULL DEFAULT 25"
        " CHECK (dim_percent >= 0 AND dim_percent <= 100)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.endpoint_settings DROP COLUMN dim_percent")
