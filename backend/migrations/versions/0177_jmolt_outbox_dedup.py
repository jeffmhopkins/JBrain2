"""jmolt outbox de-duplication: a partial unique index on (principal_id, dedup_key).

Each sitting in a jmolt night runs on fresh context and cannot see its own pending outbox,
so it re-stages actions it already queued an hour earlier — a recent night staged the SAME
upvote three times. The write tools now compute a stable `dedup_key` per action
(`vote:<target>:<dir>`, `social:<action>:<name>`, `comment:<post>:<hash>`); this index makes
a repeat a hard no-op so `OutboxRepo.stage`'s `ON CONFLICT ... DO NOTHING` swallows it.

Partial on `dedup_key IS NOT NULL`, so rows that carry no key (posts — guarded instead by the
M9 near-duplicate check — and profile updates) are unaffected, and existing rows (all of which
predate any key, so `dedup_key` is NULL) are outside the index and never collide on creation.
The RLS policies from migration 0174 are unchanged.
"""

from alembic import op

revision = "0177"
down_revision = "0176"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX jmolt_outbox_dedup"
        " ON app.jmolt_outbox (principal_id, dedup_key)"
        " WHERE dedup_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.jmolt_outbox_dedup")
