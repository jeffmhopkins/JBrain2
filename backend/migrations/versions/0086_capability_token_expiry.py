"""Capability-token lifetime: `expires_at` + `last_used_at` on principals.

The dormant `capability_token` principal kind is activated for the owner debug
console (docs/DEBUG_ACCESS.md): a revocable, time-boxed credential the owner mints
to let an external assistant run prompt iteration, read-only SQL, logs, and live
LLM routing against the box. Unlike the owner key (which never expires) and a
device key (revoked, not timed), a debug token must lapse on its own — so
authentication filters `revoked_at IS NULL AND (expires_at IS NULL OR expires_at >
now())`. `last_used_at` is stamped on each successful auth so the owner's token
list shows liveness (and a stale grant is obvious to revoke).

Both columns are nullable and only ever set for capability tokens, so existing
owner/device principals are untouched (NULL expiry = never lapses).

Revision ID: 0086
Revises: 0085
Create Date: 2026-06-22
"""

from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.principals ADD COLUMN expires_at timestamptz")
    op.execute("ALTER TABLE app.principals ADD COLUMN last_used_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE app.principals DROP COLUMN last_used_at")
    op.execute("ALTER TABLE app.principals DROP COLUMN expires_at")
