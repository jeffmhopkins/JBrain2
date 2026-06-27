"""jcode share links become single-use: bind to the first browser that opens them.

Adds ``redeemed_at`` to ``app.principals``. A share link is consumed the first time
it is redeemed (the redeem route stamps this atomically), after which the secret can
never mint another session — so the link binds to exactly one browser and a copy
forwarded to someone else is dead on arrival. The already-bound browser keeps its
scoped session cookie (a separate ``device_sessions`` row), so consuming the link
does NOT cut off the recipient — only further redemptions are blocked.

Purely additive (one NULL column; NULL for every non-share principal and for an
unredeemed share). No RLS policy change — principals visibility is already
owner-or-self under the existing policies.
"""

from alembic import op

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.principals ADD COLUMN redeemed_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE app.principals DROP COLUMN redeemed_at")
