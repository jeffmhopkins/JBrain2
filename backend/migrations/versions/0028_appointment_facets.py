"""Appointment facets — organizer / attendance / online link / description /
type on the row, and a firewall-scoped sidecar for the venue.

The where/who/org expansion of the appointment projection (docs/reference/ANALYSIS.md,
appointment.yaml). The general-domain fields (organizer, attendance mode, online
URL, description, type) ride the `appointments` row, so the row's own RLS already
gates them. The venue cannot: an `address`/`place` fact floors into the LOCATION
domain (facets.yaml `Located` 🔒), and an appointment row usually lives in
`general` — copying the venue onto it would let a non-location session read
whereabouts off a general row. So the venue lands in `app.appointment_locations`,
keyed per appointment entity and carrying its OWN domain, with the same
owner+domain RLS as the row. A reader sees the venue only when its session holds
that domain; the projector (full owner) always can. The old, never-populated
`appointments.location` column is dropped — the sidecar is the one source.

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-13
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # General-domain facets ride the appointment row (the row's RLS gates them).
    op.execute(
        """
        ALTER TABLE app.appointments
            ADD COLUMN organizer text,
            ADD COLUMN attendance_mode text,
            ADD COLUMN online_url text,
            ADD COLUMN description text,
            ADD COLUMN appointment_type text,
            DROP COLUMN location
        """
    )

    # The venue, in its own (typically location) domain — joined into a read only
    # when the session holds that domain. One row per appointment entity; cascades
    # with the entity (the purge promise) like the appointment itself.
    op.execute(
        """
        CREATE TABLE app.appointment_locations (
            entity_id uuid PRIMARY KEY REFERENCES app.entities(id) ON DELETE CASCADE,
            domain_code text NOT NULL REFERENCES app.domains(code),
            location text NOT NULL,
            source_note_id uuid REFERENCES app.notes(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.appointment_locations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.appointment_locations FORCE ROW LEVEL SECURITY")
    # Same firewall as the appointment row: owner-only AND the venue's own domain.
    # A general-scoped session that can read the appointment still cannot read a
    # location-domain venue off this table.
    op.execute(
        """
        CREATE POLICY appointment_locations_owner ON app.appointment_locations
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.appointment_locations TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.appointment_locations")
    op.execute(
        """
        ALTER TABLE app.appointments
            ADD COLUMN location text,
            DROP COLUMN organizer,
            DROP COLUMN attendance_mode,
            DROP COLUMN online_url,
            DROP COLUMN description,
            DROP COLUMN appointment_type
        """
    )
