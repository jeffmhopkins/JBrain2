"""Migration 0082 + jbrain.ops_metrics against real Timescale Postgres.

Covers the owner-only firewall on both host-metrics tables (CLAUDE.md rule 3),
the raw-sample write, the hourly rollup, the read path's resolution switch
(raw <= 2 days, hourly beyond), and time-ranged retention.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain import ops_metrics
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

NON_OWNER = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _sample(*, load: float = 0.5, gpu: float | None = 42.0, power: float | None = 14.0) -> dict:
    return {
        "mem_total_bytes": 128 << 30,
        "mem_available_bytes": 64 << 30,
        "swap_total_bytes": 8 << 30,
        "swap_free_bytes": 8 << 30,
        "disk_total_bytes": 2000 << 30,
        "disk_free_bytes": 1500 << 30,
        "load_1m": load,
        "load_5m": load,
        "load_15m": load,
        "uptime_seconds": 12345,
        "gpu_busy_percent": gpu,
        "apu_power_w": power,
        "fan_rpm": {"CPU fan": 2100, "System fan": 1850},
        "containers": [{"service": "api", "mem_bytes": 90 << 20}],
    }


_RATES = {
    "net_rx_bps": 1_000_000.0,
    "net_tx_bps": 250_000.0,
    "disk_read_bps": 5_000_000.0,
    "disk_write_bps": 2_000_000.0,
}


async def test_host_metrics_owner_only(maker: async_sessionmaker) -> None:
    """Both tables are owner-only: a non-owner capability session sees nothing."""
    await ops_metrics.store_sample(maker, OWNER, _sample())
    await ops_metrics.rollup(maker, OWNER, window=timedelta(days=1))

    for table in ("host_metrics", "host_metrics_hourly"):
        async with scoped_session(maker, OWNER) as s:
            owner_rows = (await s.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar_one()
        assert owner_rows >= 1, table
        async with scoped_session(maker, NON_OWNER) as s:
            other_rows = (await s.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar()
        assert other_rows == 0, table


async def test_store_and_read_raw(maker: async_sessionmaker) -> None:
    now = datetime.now(tz=UTC)
    for i in range(3):
        await ops_metrics.store_sample(
            maker,
            OWNER,
            _sample(load=0.4 + i),
            captured_at=now - timedelta(minutes=i),
            rates=_RATES,
        )

    out = await ops_metrics.history(
        maker, OWNER, since=now - timedelta(hours=1), until=now + timedelta(minutes=1)
    )

    assert out["resolution"] == "raw"
    assert out["points"], "expected at least one raw point"
    point = out["points"][-1]
    # mem_used = total - available = 64 GiB; fan max is the hotter of the two fans.
    assert point["mem_used_bytes"] == 64 << 30
    assert point["mem_total_bytes"] == 128 << 30
    assert point["fan_rpm_max"] == 2100
    assert point["gpu_busy_percent"] == 42.0
    assert point["power_w"] == 14.0
    # Network + disk throughput ride the raw read as bytes/sec (bucket peak).
    assert point["net_rx_bps"] == 1_000_000.0
    assert point["disk_write_bps"] == 2_000_000.0
    # Each point carries the bucket peak alongside the average line.
    assert point["mem_used_max_bytes"] == 64 << 30
    assert point["gpu_busy_max"] == 42.0
    assert point["power_w_max"] == 14.0


async def test_bucket_reports_peak_alongside_average(maker: async_sessionmaker) -> None:
    # Two samples in one bucket (identical timestamp): the line is the average but
    # the *_max carries the peak, so a spike isn't smoothed out of the history.
    at = datetime.now(tz=UTC) - timedelta(hours=3)
    await ops_metrics.store_sample(maker, OWNER, _sample(load=0.5, gpu=10.0), captured_at=at)
    await ops_metrics.store_sample(maker, OWNER, _sample(load=1.5, gpu=90.0), captured_at=at)
    out = await ops_metrics.history(
        maker, OWNER, since=at - timedelta(seconds=20), until=at + timedelta(seconds=20)
    )
    point = out["points"][-1]
    assert point["load_1m"] == 1.0  # avg of 0.5 and 1.5
    assert point["load_1m_max"] == 1.5  # the peak survives
    assert point["gpu_busy_percent"] == 50.0
    assert point["gpu_busy_max"] == 90.0


async def test_rate_columns_null_when_no_rates_given(maker: async_sessionmaker) -> None:
    # store_sample without rates (the first post-restart sample) leaves the rate
    # columns NULL, which the read surfaces as None rather than 0. Uses a distinct
    # timestamp (this module's testcontainer DB is shared across tests) so the
    # tight window sees only this row.
    at = datetime.now(tz=UTC) - timedelta(hours=6)
    await ops_metrics.store_sample(maker, OWNER, _sample(), captured_at=at)
    out = await ops_metrics.history(
        maker, OWNER, since=at - timedelta(seconds=20), until=at + timedelta(seconds=20)
    )
    assert out["points"], "expected the isolated sample"
    assert out["points"][-1]["net_rx_bps"] is None
    assert out["points"][-1]["disk_read_bps"] is None


async def test_rollup_feeds_hourly_read(maker: async_sessionmaker) -> None:
    now = datetime.now(tz=UTC)
    old = now - timedelta(days=3)
    for i in range(4):
        await ops_metrics.store_sample(
            maker,
            OWNER,
            _sample(load=1.0, gpu=float(i * 10)),
            captured_at=old + timedelta(minutes=i),
            rates=_RATES,
        )
    written = await ops_metrics.rollup(maker, OWNER, window=timedelta(days=10), now=now)
    assert written >= 1

    # A >2-day span reads the hourly rollup, not raw.
    out = await ops_metrics.history(maker, OWNER, since=now - timedelta(days=5), until=now)
    assert out["resolution"] == "hourly"
    assert out["points"], "expected a rolled-up hourly point"
    assert any(p["mem_used_bytes"] == 64 << 30 for p in out["points"])
    # APU power rolls up through the hourly path too.
    assert any(p["power_w"] == 14.0 for p in out["points"])
    # Network + disk throughput roll up through the hourly path as well.
    assert any(p["net_rx_bps"] == 1_000_000.0 for p in out["points"])
    assert any(p["disk_read_bps"] == 5_000_000.0 for p in out["points"])
    # The stored per-hour peak (gpu 0..30) reaches the hourly read's *_max band.
    assert any(p["gpu_busy_max"] == 30.0 for p in out["points"])


async def test_prune_drops_old_rows(maker: async_sessionmaker) -> None:
    now = datetime.now(tz=UTC)
    await ops_metrics.store_sample(maker, OWNER, _sample(), captured_at=now - timedelta(days=40))
    await ops_metrics.store_sample(maker, OWNER, _sample(), captured_at=now - timedelta(minutes=1))
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.host_metrics_hourly (bucket, sample_count, load_1m_avg,"
                " load_1m_max, load_5m_avg, load_15m_avg, mem_total_bytes, mem_used_avg,"
                " mem_used_max, swap_used_avg, swap_used_max, disk_total_bytes, disk_used_avg,"
                " disk_used_max) VALUES (:b, 1, 0,0,0,0, 0,0,0,0,0,0,0,0)"
            ),
            {"b": now - timedelta(days=400)},
        )

    raw_deleted, hourly_deleted = await ops_metrics.prune(maker, OWNER, now=now)

    assert raw_deleted == 1  # the 40-day-old raw sample, not the fresh one
    assert hourly_deleted == 1  # the 400-day-old bucket
    async with scoped_session(maker, OWNER) as s:
        remaining = (await s.execute(text("SELECT count(*) FROM app.host_metrics"))).scalar_one()
    assert remaining >= 1  # the fresh sample survives


# --- the columns that make an incident readable (migration 0168) -------------
#
# On 2026-08-19 this box livelocked for seven hours and needed a power cycle, and its own
# telemetry could not explain it: only `mem_available_bytes` was stored, and MemAvailable is
# exactly the number that lied — 29% used while the box had 8 GiB free and swap fully spent.
# The supervisor was already measuring the meminfo breakdown and the amdgpu counters every
# tick; they were dropped at the point of storage.

_LIVELOCK = {
    # A box in the failure state: the kernel calls 100 GiB available while 8 GiB of pages
    # are free, because ~90 GiB of page cache holds a second copy of the model weights.
    "mem_breakdown": {
        "MemFree": 8 << 30,
        "Cached": 90 << 30,
        "SReclaimable": 1 << 30,
    },
    "gpu_mem": {
        "gtt_used_bytes": 67 << 30,
        "gtt_total_bytes": 124 << 30,
        "vram_used_bytes": 2 << 30,
    },
}


async def test_the_real_memory_state_is_stored_not_just_MemAvailable(
    maker: async_sessionmaker,
) -> None:
    """MemFree, Cached and the GTT pair survive to the row, so a livelock can be read back."""
    await ops_metrics.store_sample(maker, OWNER, {**_sample(), **_LIVELOCK}, rates=_RATES)
    async with scoped_session(maker, OWNER) as session:
        row = (
            await session.execute(
                text(
                    "SELECT mem_free_bytes, mem_cached_bytes, mem_sreclaimable_bytes,"
                    " gtt_used_bytes, gtt_total_bytes, vram_used_bytes"
                    " FROM app.host_metrics ORDER BY captured_at DESC LIMIT 1"
                )
            )
        ).one()
    assert row.mem_free_bytes == 8 << 30
    assert row.mem_cached_bytes == 90 << 30
    assert row.mem_sreclaimable_bytes == 1 << 30
    assert row.gtt_used_bytes == 67 << 30
    assert row.gtt_total_bytes == 124 << 30
    assert row.vram_used_bytes == 2 << 30


async def test_a_supervisor_that_sends_none_of_it_stores_nulls(maker: async_sessionmaker) -> None:
    """An older supervisor, a non-AMD box, or a kernel missing a field must store NULL — not
    a zero, which would read as 'measured, and empty'."""
    await ops_metrics.store_sample(maker, OWNER, _sample(), rates=_RATES)
    async with scoped_session(maker, OWNER) as session:
        row = (
            await session.execute(
                text(
                    "SELECT mem_free_bytes, gtt_used_bytes FROM app.host_metrics"
                    " ORDER BY captured_at DESC LIMIT 1"
                )
            )
        ).one()
    assert row.mem_free_bytes is None and row.gtt_used_bytes is None


async def test_the_rollup_keeps_the_hours_worst_moment(maker: async_sessionmaker) -> None:
    """`mem_free_min` and `gtt_used_max`, not averages: a mean smooths away the spike that is
    the entire reason to look at the hour."""
    now = datetime.now(UTC).replace(minute=5, second=0, microsecond=0)
    roomy = {**_LIVELOCK, "mem_breakdown": {**_LIVELOCK["mem_breakdown"], "MemFree": 90 << 30}}
    await ops_metrics.store_sample(
        maker, OWNER, {**_sample(), **roomy}, rates=_RATES, captured_at=now
    )
    await ops_metrics.store_sample(
        maker,
        OWNER,
        {**_sample(), **_LIVELOCK},
        rates=_RATES,
        captured_at=now + timedelta(minutes=1),
    )
    await ops_metrics.rollup(maker, OWNER, window=timedelta(hours=2))
    async with scoped_session(maker, OWNER) as session:
        row = (
            await session.execute(
                text(
                    "SELECT mem_free_min, gtt_used_max FROM app.host_metrics_hourly"
                    " ORDER BY bucket DESC LIMIT 1"
                )
            )
        ).one()
    assert row.mem_free_min == 8 << 30  # the bad sample, not the average of the two
    assert row.gtt_used_max == 67 << 30
