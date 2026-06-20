"""`fcm_token` — per-device FCM registration for content-free push (JBrain360 M6).

A member's phone registers its FCM token here so the server can send a *content-free*
poke: the FCM payload carries no PII (no location, no names) — it only wakes the app,
which then fetches the actual notification over its authenticated channel and shows it
locally. Keeping content off Google's infrastructure is the whole point.

Device-scoped: a device manages only its own token (RLS), and a full owner / system
reads all for routing. `principal_id` cascades on the device principal's deletion;
routing additionally filters to active (non-revoked) principals, so a revoked device
stops receiving pokes (the M6 revoke-kills-token gate).

Revision ID: 0075
Revises: 0074
Create Date: 2026-06-20
"""

from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.fcm_token (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_id uuid NOT NULL REFERENCES app.principals(id) ON DELETE CASCADE,
            subject_id uuid NOT NULL REFERENCES app.subjects(id) ON DELETE CASCADE,
            domain_code text NOT NULL DEFAULT 'location' REFERENCES app.domains(code),
            token text NOT NULL UNIQUE,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.fcm_token ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.fcm_token FORCE ROW LEVEL SECURITY")
    # A device manages only its own token; a full owner / system reads all (routing).
    # WITH CHECK additionally pins a device's writes to its own subject, so a device
    # cannot register a token under another subject.
    op.execute(
        """
        CREATE POLICY fcm_token_rw ON app.fcm_token FOR ALL
        USING (
            app.has_domain_scope(domain_code)
            AND (
                app.is_full_owner()
                OR (
                    current_setting('app.principal_kind', true) = 'device_key'
                    AND principal_id::text = current_setting('app.principal_id', true)
                )
            )
        )
        WITH CHECK (
            app.has_domain_scope(domain_code)
            AND (
                app.is_full_owner()
                OR (
                    current_setting('app.principal_kind', true) = 'device_key'
                    AND principal_id::text = current_setting('app.principal_id', true)
                    AND subject_id::text = current_setting('app.subject_id', true)
                )
            )
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.fcm_token TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.fcm_token")
