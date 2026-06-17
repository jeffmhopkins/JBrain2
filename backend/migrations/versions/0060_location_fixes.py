"""The location-fixes hypertable + its subject-pinned domain firewall (Phase 7).

The continuous GPS stream is the most sensitive data class in JBrain2, so the RLS
policy is stricter than the ordinary domain firewall: a row is visible only to a
*full* owner session OR to the very device subject it belongs to. Two new pieces
make that work:

  * `app.is_full_owner()` — owner identity that is NOT owner-narrowed
    (`owner_scoped <> 'true'`). The nightly geofence sweep and ordinary owner
    reads run as a full owner and see every subject; a narrowed agent session
    does not (and a device key, being non-owner, never does).
  * the subject pin `subject_id = current_setting('app.subject_id')` — the device
    session (jbrain.db.session.device_context) carries its bound subject, so a
    device can read/write only its own fixes. A stolen device key therefore can
    neither read another subject's track nor forge fixes for one (WITH CHECK).

`has_domain_scope('location')` still gates the domain: a session without the
location scope sees zero rows regardless of identity, and the AND with the
subject pin means a non-owner capability token that merely holds the location
scope still reads nothing.

Storage: raw `latitude`/`longitude` doubles are the source of truth (verbatim
OwnTracks values, never silently mutated); `geog` is a STORED generated
`geography(Point,4326)` so metric `ST_DWithin`/`ST_Covers` and the GiST index
stay consistent with the doubles by construction. A Timescale hypertable forbids
a single-column primary key that omits the partition column, so the surrogate key
is `(id, captured_at)` and idempotent OwnTracks retries dedup on the natural key
`(subject_id, captured_at, latitude, longitude)`.

Revision ID: 0060
Revises: 0059
Create Date: 2026-06-17
"""

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Full (non-narrowed) owner identity — the subject-pin's escape hatch so the
    # owner and the system sweep see every device, but a narrowed agent does not.
    op.execute(
        """
        CREATE FUNCTION app.is_full_owner() RETURNS boolean
        LANGUAGE sql STABLE AS
        $$
          SELECT current_setting('app.principal_kind', true) = 'owner'
             AND coalesce(current_setting('app.owner_scoped', true), '') <> 'true'
        $$
        """
    )

    op.execute(
        """
        CREATE TABLE app.location_fixes (
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            subject_id uuid NOT NULL REFERENCES app.subjects(id) ON DELETE CASCADE,
            principal_id uuid REFERENCES app.principals(id),
            domain_code text NOT NULL DEFAULT 'location' REFERENCES app.domains(code),
            captured_at timestamptz NOT NULL,
            received_at timestamptz NOT NULL DEFAULT now(),
            latitude double precision NOT NULL CHECK (latitude BETWEEN -90 AND 90),
            longitude double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180),
            geog geography(Point, 4326) NOT NULL GENERATED ALWAYS AS (
                ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
            ) STORED,
            accuracy_m double precision,
            altitude_m double precision,
            velocity_mps double precision,
            course_deg double precision,
            battery_pct int,
            connection text,
            tracker_id text,
            raw jsonb,
            PRIMARY KEY (id, captured_at),
            CONSTRAINT location_fixes_natural_key
                UNIQUE (subject_id, captured_at, latitude, longitude)
        )
        """
    )

    op.execute(
        """
        SELECT create_hypertable(
            'app.location_fixes',
            by_range('captured_at', INTERVAL '7 days'),
            if_not_exists => TRUE
        )
        """
    )
    op.execute("CREATE INDEX location_fixes_geog_idx ON app.location_fixes USING gist (geog)")
    op.execute(
        "CREATE INDEX location_fixes_subject_time_idx"
        " ON app.location_fixes (subject_id, captured_at DESC)"
    )

    op.execute("ALTER TABLE app.location_fixes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.location_fixes FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY location_fixes_access ON app.location_fixes
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
    # Fixes are append-only: insert (device), read (owner/device), delete
    # (retention/purge). No UPDATE — a recorded fix is immutable.
    op.execute("GRANT SELECT, INSERT, DELETE ON app.location_fixes TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.location_fixes")
    op.execute("DROP FUNCTION IF EXISTS app.is_full_owner()")
