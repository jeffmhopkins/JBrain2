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

Renumbered from 0154 → 0156 to resolve a revision-id collision: this migration
(from PR #1010) and `0154_reports_subagent_personas` both descended from 0153 and both
claimed revision `0154`, which broke the alembic graph once both merged. It re-chains
after `0155_agent_session_plans` (the two are independent — a note column vs. a new
table — so the order is arbitrary and safe).

Revision ID: 0156
Revises: 0155
Create Date: 2026-08-06
"""

from alembic import op

revision = "0156"
down_revision = "0155"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.notes ADD COLUMN attachments_expected integer NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE app.notes DROP COLUMN attachments_expected")
