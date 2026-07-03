"""Deferred review items — a fourth review_items status (docs/reference/DESIGN.md
"Review inbox", redesign: the split-inbox triage).

The redesigned review inbox lets the owner park an item without deciding it:
"defer" (revisit later) and "discuss" (hand to the assistant) both move the
row out of the open queue into a `deferred` lane it can be pulled back from.
Unlike a resolution, deferring writes no graph effects — reopen is a bare
re-queue — so the status is the only state it needs.

The lane is a status, not a new table: review_items already carries its own
RLS and the deferred rows are the same owner's same rows, so no new isolation
surface is introduced (the existing review_items RLS policy covers it).

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-13
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_status_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_status_check"
        " CHECK (status IN ('open', 'resolved', 'dismissed', 'deferred'))"
    )
    # Mirrors review_items_open_idx: the deferred lane is read as its own list.
    op.execute(
        "CREATE INDEX review_items_deferred_idx ON app.review_items (created_at)"
        " WHERE status = 'deferred'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.review_items_deferred_idx")
    # Deferred rows have no recorded effects; returning them to the open queue
    # is the faithful inverse of a defer, and lets the narrower CHECK re-apply.
    op.execute("UPDATE app.review_items SET status = 'open' WHERE status = 'deferred'")
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_status_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_status_check"
        " CHECK (status IN ('open', 'resolved', 'dismissed'))"
    )
