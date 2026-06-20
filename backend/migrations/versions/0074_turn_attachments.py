"""`turn_attachments` — files attached to an agent CHAT turn (Stage-2 attachments).

A chat attachment is the image/PDF/text a user drags into a Full Brain chat. It is
the agent analogue of `app.attachments` (note attachments), but a NEW table because
note attachments require a non-null `note_id`: a chat file is linked to the SESSION
at upload (pre-upload, reference-by-id), and bound to the user `turn_id` only when
that turn is recorded (Stage-2 Wave 2). Hence `turn_id` is nullable.

`domain_code` carries the firewall scope (the 0002 invariant: an attachment duplicates
its owner's domain so the standard `has_domain_scope` policy needs no join). It is
computed from the session's scopes at upload (see TurnAttachmentRepo.domain_for_session):
a single-domain session stamps that domain; a multi/all/empty-scope session stamps
'general'. This is the SECURITY choice that decides which later sessions can read the
file. `has_extracts`/`has_description` mirror the note-attachment vision-cache flags
(populated in Wave 2 when OCR/caption lands).

Revision ID: 0074
Revises: 0073
Create Date: 2026-06-20
"""

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.turn_attachments (
            id uuid PRIMARY KEY,
            session_id uuid NOT NULL
                REFERENCES app.agent_sessions(id) ON DELETE CASCADE,
            -- Bound when the user turn is recorded (Wave 2); SET NULL keeps the
            -- upload row (and its blob link) alive if that turn is later deleted.
            turn_id uuid REFERENCES app.agent_turns(id) ON DELETE SET NULL,
            domain_code text NOT NULL REFERENCES app.domains(code),
            sha256 text NOT NULL,
            filename text NOT NULL,
            media_type text NOT NULL,
            size_bytes bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            has_extracts boolean NOT NULL DEFAULT false,
            has_description boolean NOT NULL DEFAULT false
        )
        """
    )
    op.execute("CREATE INDEX turn_attachments_session_idx ON app.turn_attachments (session_id)")
    op.execute("CREATE INDEX turn_attachments_turn_idx ON app.turn_attachments (turn_id)")
    op.execute("ALTER TABLE app.turn_attachments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.turn_attachments FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY turn_attachments_domain ON app.turn_attachments
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.turn_attachments TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.turn_attachments")
