"""Note attachment-intent marker: how many attachments the client will upload.

Closes the capture race behind a premature body-only integration
(docs/reference/ANALYSIS.md "Analysis gating"): a note is created by one request and
its attachments arrive on later ones, so the first ingest sees zero attachments,
emits note.ingested, and drives a blind body-only extraction that the OCR re-run
then has to redo (and, worse, briefly shows the wrong analysis). The client knows
at create time how many files it is about to upload; storing that count lets ingest
defer integration until the promised attachments land. Defaults 0 (every existing
note, and any client that does not send it, keeps today's immediate-integration
behavior). The reconciler's settle window bounds a promise that never arrives.

Revision ID: 0154
Revises: 0153
Create Date: 2026-08-06
"""

from alembic import op

revision = "0154"
down_revision = "0153"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.notes ADD COLUMN attachments_expected integer NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE app.notes DROP COLUMN attachments_expected")
