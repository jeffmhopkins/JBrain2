"""Host-metrics time-series: a raw 30s sample hypertable + an hourly rollup.

The Ops screen shows live host vitals (mem/disk/load/gpu/fans/containers) but
keeps nothing — every read is a point-in-time snapshot. This adds durable
history so the operator (and the agent) can answer "has it been throttling /
heating / leaking memory overnight?".

Two owner-only Timescale hypertables, written by the worker (the singleton
background loop — see jbrain.ops_metrics):

  * `host_metrics` — one row per ~30s sample, full fidelity (per-container
    memory + per-fan RPM as jsonb, plus a scalar `fan_rpm_max` so the hottest
    fan graphs/rolls up without unpacking jsonb). Raw resolution, kept 30 days.
  * `host_metrics_hourly` — one row per clock hour with avg/extreme rollups of
    the scalar series, kept ~1 year. Populated app-side by the worker (the
    codebase schedules app-side, not via Timescale background jobs, which the
    test harness disables anyway), so retention is plain time-ranged DELETEs in
    the worker rather than add_retention_policy.

Both are owner-only (host telemetry is not domain data): `app.is_owner()`,
matching the wiki_articles spine. A hypertable forbids a single-column PK that
omits the partition column, so the raw key is `(id, captured_at)`; the hourly
table's partition column `bucket` is itself the natural key, so it is the PK.

Revision ID: 0082
Revises: 0081
Create Date: 2026-06-22
"""

from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.host_metrics (
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            captured_at timestamptz NOT NULL DEFAULT now(),
            mem_total_bytes bigint NOT NULL,
            mem_available_bytes bigint NOT NULL,
            swap_total_bytes bigint NOT NULL,
            swap_free_bytes bigint NOT NULL,
            disk_total_bytes bigint NOT NULL,
            disk_free_bytes bigint NOT NULL,
            load_1m double precision NOT NULL,
            load_5m double precision NOT NULL,
            load_15m double precision NOT NULL,
            uptime_seconds bigint NOT NULL,
            gpu_busy_percent double precision,
            fan_rpm_max integer,
            fan_rpm jsonb,
            containers jsonb,
            PRIMARY KEY (id, captured_at)
        )
        """
    )
    op.execute(
        """
        SELECT create_hypertable(
            'app.host_metrics',
            by_range('captured_at', INTERVAL '1 day'),
            if_not_exists => TRUE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE app.host_metrics_hourly (
            bucket timestamptz NOT NULL,
            sample_count integer NOT NULL,
            load_1m_avg double precision NOT NULL,
            load_1m_max double precision NOT NULL,
            load_5m_avg double precision NOT NULL,
            load_15m_avg double precision NOT NULL,
            mem_total_bytes bigint NOT NULL,
            mem_used_avg bigint NOT NULL,
            mem_used_max bigint NOT NULL,
            swap_used_avg bigint NOT NULL,
            swap_used_max bigint NOT NULL,
            disk_total_bytes bigint NOT NULL,
            disk_used_avg bigint NOT NULL,
            disk_used_max bigint NOT NULL,
            gpu_busy_avg double precision,
            gpu_busy_max double precision,
            fan_rpm_avg double precision,
            fan_rpm_max integer,
            PRIMARY KEY (bucket)
        )
        """
    )
    op.execute(
        """
        SELECT create_hypertable(
            'app.host_metrics_hourly',
            by_range('bucket', INTERVAL '30 days'),
            if_not_exists => TRUE
        )
        """
    )

    for table in ("host_metrics", "host_metrics_hourly"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_owner ON app.{table}
            USING (app.is_owner()) WITH CHECK (app.is_owner())
            """
        )
        # The worker inserts samples + upserts/prunes rollups; the owner reads.
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.host_metrics_hourly")
    op.execute("DROP TABLE app.host_metrics")
