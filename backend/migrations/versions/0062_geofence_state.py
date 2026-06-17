"""`geofence_state` — per-(subject, fence) hysteresis state for transition debounce.

Operational state the inline detector reads-modifies-writes on each fix: how many
consecutive confirming fixes have accrued and whether the subject is currently
inside/outside/unknown for a given fence. Unlike the geometry mirror (0061), this
IS written by the device session at ingest, so it carries the full subject-pinned
firewall (a device writes only its own state; a full owner / the sweep sees all).

Revision ID: 0062
Revises: 0061
Create Date: 2026-06-17
"""

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.geofence_state (
            subject_id uuid NOT NULL REFERENCES app.subjects(id) ON DELETE CASCADE,
            place_geofence_id uuid NOT NULL
                REFERENCES app.place_geofence(id) ON DELETE CASCADE,
            domain_code text NOT NULL DEFAULT 'location' REFERENCES app.domains(code),
            state text NOT NULL DEFAULT 'unknown'
                CHECK (state IN ('inside', 'outside', 'unknown')),
            confirming_fixes int NOT NULL DEFAULT 0,
            since timestamptz,
            last_fix_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (subject_id, place_geofence_id)
        )
        """
    )

    op.execute("ALTER TABLE app.geofence_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.geofence_state FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY geofence_state_access ON app.geofence_state
        USING (
            app.has_domain_scope(domain_code)
            AND (
                app.is_full_owner()
                OR subject_id::text = current_setting('app.subject_id', true)
            )
        )
        WITH CHECK (
            app.has_domain_scope(domain_code)
            AND (
                app.is_full_owner()
                OR subject_id::text = current_setting('app.subject_id', true)
            )
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.geofence_state TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.geofence_state")
