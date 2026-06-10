"""Notes and attachments, the first domain-firewalled user data.

RLS via app.has_domain_scope(domain_code) — the pattern proven by the Phase 0
firewall-probe test. Attachments duplicate domain_code so their policy needs
no join.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-10
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.notes (
            id uuid PRIMARY KEY,
            client_id text NOT NULL UNIQUE,
            domain_code text NOT NULL REFERENCES app.domains(code),
            destination text,
            body text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            deleted_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX notes_created_at_idx ON app.notes (created_at DESC)")
    op.execute("CREATE INDEX notes_domain_idx ON app.notes (domain_code)")

    op.execute(
        """
        CREATE TABLE app.attachments (
            id uuid PRIMARY KEY,
            note_id uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
            domain_code text NOT NULL REFERENCES app.domains(code),
            sha256 text NOT NULL,
            filename text NOT NULL,
            media_type text NOT NULL,
            size_bytes bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX attachments_note_idx ON app.attachments (note_id)")

    for table in ("notes", "attachments"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_domain ON app.{table}
            USING (app.has_domain_scope(domain_code))
            WITH CHECK (app.has_domain_scope(domain_code))
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON app.{table} TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.attachments")
    op.execute("DROP TABLE app.notes")
