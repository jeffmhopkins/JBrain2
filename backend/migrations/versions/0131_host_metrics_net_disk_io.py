"""Host-metrics: network + disk-I/O throughput series.

The Ops history graphs cover mem/load/gpu/power but not I/O, so "was it saturating
the uplink / thrashing the disk overnight?" had no answer. Add four throughput
series — network rx/tx and disk read/write, in bytes per second.

The supervisor exposes only the kernel's *cumulative* byte counters
(/proc/net/dev, /proc/diskstats); the worker's sampler turns the delta between
consecutive ticks into a rate and stores THAT, so these columns are scalar
bytes/sec — uniform with gpu_busy_percent, graphing and rolling up the same way.
All nullable: the first sample after a (re)start has no prior counter to diff, and
a counter reset (reboot) yields a null rather than a bogus negative spike.

Revision ID: 0131
Revises: 0130
Create Date: 2026-07-17
"""

from alembic import op

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None

# The raw scalar rate columns and their hourly avg/max rollup counterparts.
_RAW_COLS = ("net_rx_bps", "net_tx_bps", "disk_read_bps", "disk_write_bps")


def upgrade() -> None:
    for col in _RAW_COLS:
        op.execute(f"ALTER TABLE app.host_metrics ADD COLUMN {col} double precision")
    for col in _RAW_COLS:
        op.execute(
            f"ALTER TABLE app.host_metrics_hourly ADD COLUMN {col}_avg double precision"
        )
        op.execute(
            f"ALTER TABLE app.host_metrics_hourly ADD COLUMN {col}_max double precision"
        )


def downgrade() -> None:
    for col in _RAW_COLS:
        op.execute(f"ALTER TABLE app.host_metrics DROP COLUMN {col}")
        op.execute(f"ALTER TABLE app.host_metrics_hourly DROP COLUMN {col}_avg")
        op.execute(f"ALTER TABLE app.host_metrics_hourly DROP COLUMN {col}_max")
