"""Capability-token suspend: `suspended_at` on principals.

Adds a *reversible* pause to the debug-console token lifecycle
(docs/DEBUG_ACCESS.md). Revocation is permanent (`revoked_at`), and expiry lapses
on its own (`expires_at`); suspend sits between them — the owner (or the console
itself) freezes a token so it stops authenticating, then the owner un-freezes it
later from the PWA token list. Authentication therefore also filters
`suspended_at IS NULL`, so a suspended token fails closed exactly like a revoked
one, but `resume` can clear the stamp and bring it back.

A suspended token cannot un-suspend itself (it can no longer authenticate the
`/api/debug/*` surface), so resume is owner-gated only — the safe asymmetry.

The column is nullable and only ever set for capability tokens, so existing
owner/device principals are untouched (NULL = never suspended).

Revision ID: 0087
Revises: 0086
Create Date: 2026-06-22
"""

from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.principals ADD COLUMN suspended_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE app.principals DROP COLUMN suspended_at")
