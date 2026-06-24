"""The owner's last-known PWA fix cache — a single row per owner, location firewall.

`current_location` answers jerv from the warm geolocation fix the PWA attaches to
the turn (turn-local, never the OwnTracks device stack). When a turn arrives with
no fix (geolocation denied, a desktop/background session), there was previously
nothing to fall back on. This table caches the most recent warm fix so a fixless
turn can answer from the last known position — clearly labelled with its age, never
as "here now".

It is location data, so it lives behind the SAME stricter-than-domain firewall as
`location_fixes` (CLAUDE.md rule 3): a row is readable/writable only by a *full*
owner session that also holds the `location` scope. A narrowed agent session, a
device key, or a non-location capability token sees nothing and cannot write —
RLS fails it closed. One row per owner principal (the warm fix has no device
subject); the API upserts it under the full-owner ctx on every fix-bearing turn.

Revision ID: 0090
Revises: 0089
Create Date: 2026-06-24
"""

from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.owner_last_fix (
            principal_id uuid PRIMARY KEY REFERENCES app.principals(id) ON DELETE CASCADE,
            domain_code text NOT NULL DEFAULT 'location' REFERENCES app.domains(code),
            captured_at timestamptz NOT NULL DEFAULT now(),
            latitude double precision NOT NULL CHECK (latitude BETWEEN -90 AND 90),
            longitude double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180)
        )
        """
    )

    op.execute("ALTER TABLE app.owner_last_fix ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.owner_last_fix FORCE ROW LEVEL SECURITY")
    # Same firewall as location_fixes minus the device-subject branch: the warm fix
    # is the owner's own, so only a *full* owner (not a narrowed agent) holding the
    # location scope may touch it. A device key / scoped token never qualifies.
    op.execute(
        """
        CREATE POLICY owner_last_fix_access ON app.owner_last_fix
        USING (app.has_domain_scope(domain_code) AND app.is_full_owner())
        WITH CHECK (app.has_domain_scope(domain_code) AND app.is_full_owner())
        """
    )
    # Upserted in place each fix-bearing turn (INSERT … ON CONFLICT DO UPDATE), so
    # UPDATE is granted here unlike the append-only fixes table.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.owner_last_fix TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.owner_last_fix")
