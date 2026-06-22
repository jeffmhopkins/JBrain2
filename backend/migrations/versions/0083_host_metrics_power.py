"""Add APU/SoC package power (watts) to the host-metrics time series.

The supervisor now reads the amdgpu hwmon's `power1_average` — on Strix Halo the
CPU+iGPU share one die, so this is the whole-APU draw (the dominant consumer,
though not wall power). Recorded alongside the other vitals: a raw `power_w` and
its hourly avg/max rollup. Nullable — rows sampled before this migration (and any
host without an amdgpu power sensor) simply carry NULL, which the readers skip.

Revision ID: 0083
Revises: 0082
Create Date: 2026-06-22
"""

from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.host_metrics ADD COLUMN power_w double precision")
    op.execute(
        "ALTER TABLE app.host_metrics_hourly"
        " ADD COLUMN power_w_avg double precision,"
        " ADD COLUMN power_w_max double precision"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.host_metrics DROP COLUMN power_w")
    op.execute(
        "ALTER TABLE app.host_metrics_hourly DROP COLUMN power_w_avg, DROP COLUMN power_w_max"
    )
