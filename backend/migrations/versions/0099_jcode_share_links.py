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
    # Widen the kind CHECK (from 0001) to admit the share-link kind.
    op.execute("ALTER TABLE app.principals DROP CONSTRAINT principals_kind_check")
    op.execute(
        "ALTER TABLE app.principals ADD CONSTRAINT principals_kind_check "
        "CHECK (kind IN ('owner', 'capability_token', 'device_key', 'jcode_share_link'))"
    )


def downgrade() -> None:
    # Drop any share-link rows first, or the narrowed CHECK below would reject them.
    op.execute("DELETE FROM app.principals WHERE kind = 'jcode_share_link'")
    op.execute("ALTER TABLE app.principals DROP CONSTRAINT principals_kind_check")
    op.execute(
        "ALTER TABLE app.principals ADD CONSTRAINT principals_kind_check "
        "CHECK (kind IN ('owner', 'capability_token', 'device_key'))"
    )
    op.execute("ALTER TABLE app.principals DROP COLUMN jcode_session_id")
