"""The who-saw-whom access log (JBrain360 M3a).

Every dashboard read (and, later, every live delivery / poke) of a subject's
location writes a `view_audit` row. With the consent gate off and retention off
(owner decisions T7), this append-only log is the heightened-importance
accountability control: it records *who viewed whom, when*.

RLS: a full owner reads all; a subject reads rows where it is the **target** (its
own "who can see me / who looked") and rows it authored as **viewer**. Writes are
attributable: the owner may write any row; a device may only write a row that
attributes the view to its own subject (no forging another's view). Append-only —
no UPDATE/DELETE grant.

Revision ID: 0069
Revises: 0068
Create Date: 2026-06-18
"""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.view_audit (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            viewer_subject_id uuid,            -- NULL for the owner (no subject)
            viewer_principal_id uuid,
            target_subject_id uuid NOT NULL REFERENCES app.subjects(id) ON DELETE CASCADE,
            path text NOT NULL CHECK (path IN ('history', 'live', 'poke')),
            domain_code text NOT NULL DEFAULT 'location' REFERENCES app.domains(code),
            at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX view_audit_target_idx ON app.view_audit (target_subject_id, at DESC)")
    op.execute("CREATE INDEX view_audit_viewer_idx ON app.view_audit (viewer_subject_id, at DESC)")

    op.execute("ALTER TABLE app.view_audit ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.view_audit FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY view_audit_read ON app.view_audit FOR SELECT
        USING (
            app.has_domain_scope(domain_code)
            AND (
                app.is_full_owner()
                OR target_subject_id::text = current_setting('app.subject_id', true)
                OR viewer_subject_id::text = current_setting('app.subject_id', true)
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY view_audit_write ON app.view_audit FOR INSERT
        WITH CHECK (
            app.has_domain_scope(domain_code)
            AND (
                app.is_full_owner()
                OR (
                    current_setting('app.principal_kind', true) = 'device_key'
                    AND viewer_subject_id::text = current_setting('app.subject_id', true)
                )
            )
        )
        """
    )
    op.execute("GRANT SELECT, INSERT ON app.view_audit TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.view_audit")
