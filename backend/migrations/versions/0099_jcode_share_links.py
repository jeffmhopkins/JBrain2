"""jcode share links: bind a capability-style principal to one code-mode session.

A share link lets the owner open a single jcode session on any browser (D2). It
reuses the debug capability-token machinery (a `principals` row with `key_hash` +
`expires_at` + `revoked_at`), adding one column — `jcode_session_id` — so the grant
is scoped to exactly one session: the redeem + every operational route checks that
the principal's bound session id matches the route's. The column is NULL for every
other principal kind, so this is purely additive (no RLS policy change: principals
visibility is already owner-or-self under the existing policies).
"""

from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.principals ADD COLUMN jcode_session_id text")


def downgrade() -> None:
    op.execute("ALTER TABLE app.principals DROP COLUMN jcode_session_id")
