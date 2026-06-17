"""Enable the spatial engines for Phase 7 location ingestion.

TimescaleDB (hypertables for the high-frequency location stream) and PostGIS
(geofence geometry + metric `ST_DWithin`/`ST_Covers`) both ship in the
`timescale/timescaledb-ha` image production runs and the integration suite now
uses, but the extensions are per-database, so a freshly created database (every
test clone, a new prod deploy) must create them explicitly. Isolated in its own
migration so the spatial enablement is one reviewable, revertible unit.

Migrations run as a superuser (like 0003's pgvector), which `CREATE EXTENSION`
requires; `timescaledb` additionally needs its preloaded library, present in the
HA image. `IF NOT EXISTS` keeps this idempotent where the image already created
them in the default database.

Downgrade is intentionally a no-op: the extensions are shared infrastructure that
ships with the image and may back objects outside this feature; the dependent
location tables are dropped by the 0061–0063 downgrades via the revision chain.

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
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")


def downgrade() -> None:
    # No-op on purpose — see module docstring. Dropping a shared extension here
    # would risk cascading away unrelated objects.
    pass
