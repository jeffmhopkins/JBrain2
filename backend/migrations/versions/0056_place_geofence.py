"""`place_geofence` — a derived spatial read-model of the Place geofence predicate.

The canonical geofence geometry is a note-sourced graph fact: the `geofence`
predicate on a Place entity (schema/defs/types/place.yaml). That JSON-shaped fact
cannot be `ST_DWithin`-queried at ingest speed, so this table is its **derived,
non-authoritative spatial mirror** — projected from the graph at fact-apply time
(and rebuilt by the sweep), never edited directly. The UI "geofence editor" files
a place/correction note; it does not write this table. Hence the firewall lets a
device READ applicable fences (its own subject's, plus subject-less "all devices"
fences) but only a full owner / system projector WRITE the mirror (WITH CHECK).

`place_entity_id` cascades on entity purge — the privacy promise that nothing
derived from a deleted Place survives.

Revision ID: 0056
Revises: 0055
Create Date: 2026-06-17
"""

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.place_geofence (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            place_entity_id uuid NOT NULL REFERENCES app.entities(id) ON DELETE CASCADE,
            subject_id uuid REFERENCES app.subjects(id) ON DELETE CASCADE,
            domain_code text NOT NULL DEFAULT 'location' REFERENCES app.domains(code),
            name text NOT NULL DEFAULT '',
            center geography(Point, 4326),
            radius_m double precision,
            polygon geography(Polygon, 4326),
            enabled boolean NOT NULL DEFAULT true,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT place_geofence_shape CHECK (
                (center IS NOT NULL AND radius_m IS NOT NULL AND polygon IS NULL)
                OR (polygon IS NOT NULL AND center IS NULL AND radius_m IS NULL)
            )
        )
        """
    )
    op.execute("CREATE INDEX place_geofence_center_idx ON app.place_geofence USING gist (center)")
    op.execute("CREATE INDEX place_geofence_polygon_idx ON app.place_geofence USING gist (polygon)")

    op.execute("ALTER TABLE app.place_geofence ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.place_geofence FORCE ROW LEVEL SECURITY")
    # Read: full owner / system, or a device for fences applicable to it (its own
    # subject, or a subject-less "all devices" fence). Write: full owner / system
    # only — the projector owns this mirror; devices never write it.
    op.execute(
        """
        CREATE POLICY place_geofence_read ON app.place_geofence FOR SELECT
        USING (
            app.has_domain_scope(domain_code)
            AND (
                app.is_full_owner()
                OR (
                    current_setting('app.principal_kind', true) = 'device_key'
                    AND (
                        subject_id IS NULL
                        OR subject_id::text = current_setting('app.subject_id', true)
                    )
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY place_geofence_write ON app.place_geofence
        FOR ALL
        USING (app.has_domain_scope(domain_code) AND app.is_full_owner())
        WITH CHECK (app.has_domain_scope(domain_code) AND app.is_full_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.place_geofence TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.place_geofence")
