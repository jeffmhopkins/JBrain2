"""Give notes a server-side receipt time, so the attachment settle window is real.

`notes.created_at` is the CLIENT's capture time: `SqlNotesRepo.create_note` writes the
client's instant when one is supplied, because the offline outbox flushes later and
server `now()` would misdate the capture. The integration reconciler's attachment
settle window (`queue.backfill_pending_integration`) compared against that column, so a
note captured offline yesterday and flushed today was already past the window the
instant it arrived — the gate that exists to wait for a promised attachment was
defeated in exactly the case it was written for, and the note integrated body-only
before its image could land.

`received_at` is the instant the row was INSERTed and is never client-settable, so the
window measures the upload race it is actually about. Backfilled from `created_at`:
that is the only evidence an existing row carries, and for an online capture the two
are the same instant anyway.

RLS: a column on an existing table, so `app.notes`'s domain-firewalled policy and
grants from 0003 cover it unchanged — no new policy, no new grant, and no widening (a
narrowed reader that could not see the note still cannot see its receipt time). It
carries no content beyond a timestamp already implicit in the row's existence.

Revision ID: 0189
Revises: 0188
Create Date: 2026-09-08
"""

from alembic import op

revision = "0189"
down_revision = "0188"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.notes ADD COLUMN received_at timestamptz")
    # Backfill before NOT NULL: a plain `DEFAULT now()` on the ADD would stamp every
    # historical note with the migration's instant and hand the whole corpus a fresh
    # settle window.
    op.execute("UPDATE app.notes SET received_at = created_at WHERE received_at IS NULL")
    op.execute("ALTER TABLE app.notes ALTER COLUMN received_at SET DEFAULT now()")
    op.execute("ALTER TABLE app.notes ALTER COLUMN received_at SET NOT NULL")
    # The reconciler's settle predicate scans un-integrated notes by receipt time.
    op.execute(
        "CREATE INDEX notes_received_at_idx ON app.notes (received_at)"
        " WHERE deleted_at IS NULL AND integration_state <> 'integrated'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.notes_received_at_idx")
    op.execute("ALTER TABLE app.notes DROP COLUMN received_at")
