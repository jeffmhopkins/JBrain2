"""Vision-extract cache for attachments (docs/ANALYSIS.md "Attachments").

app.attachment_extracts holds the products of the vision backends — OCR
transcriptions and captions are separate products [decided] — keyed by
attachment. Ingest reads this table as a pure cache (capture-to-searchable
never waits on a cloud LLM), and the async ocr_attachment job is the only
writer. `tool` records provider:model so re-OCR after a tool upgrade is a
targeted job over the old tool's rows; re-OCR is delete + insert, the chunks
pattern, hence the DELETE grant. Confidence is honest and capped: OCR-derived
text never claims more than 0.7 ("Guards" — low-confidence health values must
not auto-supersede).

domain_code duplicates the attachment's domain (the 0002 invariant) so the
standard has_domain_scope policy needs no join.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-11
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.attachment_extracts (
            id uuid PRIMARY KEY,
            attachment_id uuid NOT NULL REFERENCES app.attachments(id) ON DELETE CASCADE,
            kind text NOT NULL CHECK (kind IN ('ocr', 'caption')),
            -- provider:model that produced the text, e.g. "xai:grok-4.3".
            tool text NOT NULL,
            text text NOT NULL,
            confidence real,
            -- Segment provenance (filename for whole-image, "page N" once the
            -- PDF scan path routes pages through this cache).
            source_anchor text,
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX attachment_extracts_attachment_idx ON app.attachment_extracts (attachment_id)"
    )
    op.execute("ALTER TABLE app.attachment_extracts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.attachment_extracts FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY attachment_extracts_domain ON app.attachment_extracts
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON app.attachment_extracts TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.attachment_extracts")
