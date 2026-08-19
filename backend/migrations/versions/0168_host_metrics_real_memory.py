"""Record what the box's memory was ACTUALLY doing, not just what it claimed was available.

On 2026-08-19 this host livelocked for seven hours and needed a power cycle. Afterwards its
own telemetry could not explain it: `app.host_metrics` stores `mem_available_bytes` and
nothing else about memory, and MemAvailable is precisely the number that was lying. It read
29% used through the entire failure while the box had 8 GiB of free pages and 100%-consumed
swap. The recorded history of the incident shows a comfortable machine.

The cause was a second, invisible copy of every model's weights: the gateway serves
`--no-mmap`, so llama.cpp reads each GGUF instead of mapping it and the weights end up in
GTT *and* in the page cache. Unload frees the GTT copy and leaves the other. Measured on the
box, a single gpt-oss-120b load put `Cached` at 49.1 GiB with `MemFree` at 8.4 of 121, and
the cache copy outlived the unload.

Every field added here was already being measured — the supervisor reads the full
/proc/meminfo breakdown and the amdgpu `mem_info_*` counters and ships them on `/metrics`
each tick — and then thrown away at the point of storage. So this is not new instrumentation;
it is keeping what the box already knows about itself, so the next incident can be read from
the record instead of reproduced on a live host.

Nullable throughout: existing rows predate the columns, and a box whose kernel or GPU does
not expose a field stores NULL rather than a zero that would read as "measured, and empty".

Revision ID: 0168
Revises: 0167
"""

import sqlalchemy as sa
from alembic import op

revision = "0168"
down_revision = "0167"
branch_labels = None
depends_on = None


# (column, type, why it is worth a byte)
_COLUMNS = (
    # The number the budget should have been reading all along. `MemAvailable - MemFree` is
    # the reclaimable cache, i.e. exactly the quantity that hid the failure.
    ("mem_free_bytes", sa.BigInteger()),
    # The page cache. On this box it is dominated by the weights copy, so its size IS the
    # size of the problem.
    ("mem_cached_bytes", sa.BigInteger()),
    # Dentry/inode slab: genuinely cheap to reclaim, and credited as free by the budget
    # (jbrain.host_metrics.read_memory_gb). Stored so that credit can be audited.
    ("mem_sreclaimable_bytes", sa.BigInteger()),
    # GTT is the pool a loaded model actually occupies on this APU, drawn from the same
    # system RAM. Without it a memory chart cannot show a model at all — the models are the
    # single biggest consumer on the box and were entirely absent from its history.
    ("gtt_used_bytes", sa.BigInteger()),
    ("gtt_total_bytes", sa.BigInteger()),
    ("vram_used_bytes", sa.BigInteger()),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("host_metrics", sa.Column(name, type_, nullable=True), schema="app")
    # The hourly rollup keeps the peak of the two that matter for "was the box in trouble":
    # an average would smooth away exactly the spike worth finding.
    op.add_column(
        "host_metrics_hourly",
        sa.Column("mem_free_min", sa.BigInteger(), nullable=True),
        schema="app",
    )
    op.add_column(
        "host_metrics_hourly",
        sa.Column("gtt_used_max", sa.BigInteger(), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("host_metrics_hourly", "gtt_used_max", schema="app")
    op.drop_column("host_metrics_hourly", "mem_free_min", schema="app")
    for name, _type in reversed(_COLUMNS):
        op.drop_column("host_metrics", name, schema="app")
