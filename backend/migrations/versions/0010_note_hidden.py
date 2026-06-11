"""Hide notes from the entry stream without deleting them.

hidden_at mirrors the deleted_at soft-delete convention but with a different
meaning: a hidden note is removed from the home stream while its chunks and
embeddings stay intact, so it remains findable in Search and openable from
there. NULL = visible; an instant records when it was hidden (undo clears it).

A partial index serves the stream's hot path — newest-first over the visible,
non-deleted rows.

Rides the existing app.notes RLS policy (no new table); the notes RLS
isolation test exercises set_hidden across the firewall.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-11
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.notes ADD COLUMN hidden_at timestamptz")
    op.execute(
        "CREATE INDEX notes_stream_idx ON app.notes (created_at DESC)"
        " WHERE deleted_at IS NULL AND hidden_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.notes_stream_idx")
    op.execute("ALTER TABLE app.notes DROP COLUMN hidden_at")
