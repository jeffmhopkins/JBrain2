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


def _sample(*, load: float = 0.5, gpu: float | None = 42.0) -> dict:
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
        "fan_rpm": {"CPU fan": 2100, "System fan": 1850},
        "containers": [{"service": "api", "mem_bytes": 90 << 20}],
    }


async def test_host_metrics_owner_only(maker: async_sessionmaker) -> None:
    """Both tables are owner-only: a non-owner capability session sees nothing."""
    await ops_metrics.store_sample(maker, OWNER, _sample())
    await ops_metrics.rollup(maker, OWNER, window=timedelta(days=1))

    for table in ("host_metrics", "host_metrics_hourly"):
        async with scoped_session(maker, OWNER) as s:
            owner_rows = (await s.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar()
        assert owner_rows >= 1, table
        async with scoped_session(maker, NON_OWNER) as s:
            other_rows = (await s.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar()
        assert other_rows == 0, table


async def test_store_and_read_raw(maker: async_sessionmaker) -> None:
    now = datetime.now(tz=UTC)
    for i in range(3):
        await ops_metrics.store_sample(
            maker, OWNER, _sample(load=0.4 + i), captured_at=now - timedelta(minutes=i)
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


async def test_rollup_feeds_hourly_read(maker: async_sessionmaker) -> None:
    now = datetime.now(tz=UTC)
    old = now - timedelta(days=3)
    for i in range(4):
        await ops_metrics.store_sample(
            maker, OWNER, _sample(load=1.0, gpu=float(i * 10)),
            captured_at=old + timedelta(minutes=i),
        )
    written = await ops_metrics.rollup(maker, OWNER, window=timedelta(days=10), now=now)
    assert written >= 1

    # A >2-day span reads the hourly rollup, not raw.
    out = await ops_metrics.history(maker, OWNER, since=now - timedelta(days=5), until=now)
    assert out["resolution"] == "hourly"
    assert out["points"], "expected a rolled-up hourly point"
    assert any(p["mem_used_bytes"] == 64 << 30 for p in out["points"])


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
        remaining = (await s.execute(text("SELECT count(*) FROM app.host_metrics"))).scalar()
    assert remaining >= 1  # the fresh sample survives
