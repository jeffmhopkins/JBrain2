"""DELETE grants for the note-deletion purge (jbrain.analysis.purge).

0006 and 0007 deliberately withheld DELETE on these tables, with comments
saying facts are never deleted (the supersession chain IS the revision
history), review history is kept, and note_analysis only ever dies with its
note. That doctrine stands for normal operation — and now carries its ONE
sanctioned exception [decided]: when a source note is deleted. Notes are the
sole sources of truth, and deleting one is a privacy promise, so the app
hard-deletes everything derived from the note — its facts, temporal tokens,
review items in any status (resolved history quotes the note's text in
frozen snippets), its note_analysis row, and provisional entities no
surviving note references — and repairs the supersession chains the purge
cuts. Read 0006's "never DELETE" grant justifications and 0007's "never
DELETE (the cascade from notes is the only removal path)" as amended by this
exception; historical migrations are not edited.

Unchanged on purpose: entity_mentions and entity_aliases already carry
DELETE from 0006 (the re-extraction rebuild path), and entity_distinctions
stays insert-only — negative knowledge is permanent even across note
deletion.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-11
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

PURGE_TABLES = ("facts", "temporal_tokens", "review_items", "note_analysis", "entities")


def upgrade() -> None:
    for table in PURGE_TABLES:
        op.execute(f"GRANT DELETE ON app.{table} TO jbrain_app")


def downgrade() -> None:
    for table in PURGE_TABLES:
        op.execute(f"REVOKE DELETE ON app.{table} FROM jbrain_app")
