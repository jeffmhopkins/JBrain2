"""Host-metrics time-series: sampling, hourly rollup, retention, and reads.

The worker (the singleton background loop) samples the supervisor's `/metrics`
every ~30s into `app.host_metrics`, rolls completed clock hours up into
`app.host_metrics_hourly`, and prunes both on their retention windows. The Ops
history endpoint and the agent's `query_server_metrics` tool read through
`history()`. Every write runs under the owner `SYSTEM_CTX`; the tables are
owner-only (migration 0082), so the RLS policy is the real gate.

Retention and rollup are app-side (plain SQL on the worker tick) rather than
Timescale background policies: the codebase schedules app-side throughout, and
the test harness disables Timescale's background workers — so a policy would be
both out of step and untestable. The cost is trivial at personal scale (30 days
of 30s samples is ~10^5 rows).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, text

from jbrain.db.session import scoped_session

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.db.session import SessionContext

# Retention windows (the owner's choice: generous raw + a one-year tail).
RAW_RETENTION = timedelta(days=30)
HOURLY_RETENTION = timedelta(days=365)

# A history query at/under this span reads raw 30s rows (downsampled to fit);
# wider spans read the hourly rollup, so a year-long chart never scans 10^6 rows.
RAW_QUERY_MAX = timedelta(days=2)

# Target point count for a history series — enough to read a trend, small enough
# to keep the payload light and the SVG path cheap.
MAX_POINTS = 300

# The sampler walks back this far each tick when refreshing the rollup, so the
# current partial hour and the one before it stay fresh between restarts; a boot
# pass covers the full window in case the worker was down longer.
ROLLUP_WINDOW = timedelta(hours=3)

_SAMPLE_INTERVAL_SECONDS = 30


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def fan_rpm_max_of(fan_rpm: dict[str, int] | None) -> int | None:
    """The hottest fan's RPM, or None when the host reports no fans — lifted to a
    scalar at write time so it graphs and rolls up without unpacking the jsonb."""
    if not fan_rpm:
        return None
    return max(fan_rpm.values())


# Each throughput series: the stored rate column -> (metrics section, counter field
# within it). The supervisor reports monotonic byte counters; the tracker below
# diffs them into bytes/sec so history graphs a rate, not an ever-climbing total.
_RATE_SOURCES: dict[str, tuple[str, str]] = {
    "net_rx_bps": ("net", "rx_bytes"),
    "net_tx_bps": ("net", "tx_bytes"),
    "disk_read_bps": ("disk_io", "read_bytes"),
    "disk_write_bps": ("disk_io", "write_bytes"),
}


def _counters(metrics: dict[str, Any]) -> dict[str, int | None]:
    """Pull the cumulative byte counters out of a `/metrics` payload, one per rate
    series — None for a series the supervisor didn't report (an older build, or a
    read that failed), which the tracker turns into a null rate."""
    out: dict[str, int | None] = {}
    for col, (section, field) in _RATE_SOURCES.items():
        block = metrics.get(section)
        value = block.get(field) if isinstance(block, dict) else None
        out[col] = value if isinstance(value, int) else None
    return out


class RateTracker:
    """Turns the supervisor's monotonic byte counters into per-second throughput.

    The worker holds ONE instance across sampling ticks; each `rates(now_s,
    metrics)` diffs the current counters against the previous successful sample.
    The first call (no prior), a non-positive time delta, and any counter that went
    backwards (a reboot reset, or the field dropping out) yield None for that series
    — never a bogus negative or divide-by-zero spike. Only successful samples
    advance the baseline, so a missed tick just widens the next real interval."""

    def __init__(self) -> None:
        self._prev_s: float | None = None
        self._prev: dict[str, int] = {}

    def rates(self, now_s: float, metrics: dict[str, Any]) -> dict[str, float | None]:
        current = _counters(metrics)
        out: dict[str, float | None] = dict.fromkeys(_RATE_SOURCES)
        dt = None if self._prev_s is None else now_s - self._prev_s
        if dt is not None and dt > 0:
            for col in _RATE_SOURCES:
                cur, old = current[col], self._prev.get(col)
                if cur is not None and old is not None and cur >= old:
                    out[col] = (cur - old) / dt
        self._prev_s = now_s
        self._prev = {k: v for k, v in current.items() if v is not None}
        return out


async def fetch_supervisor_metrics(client: httpx.AsyncClient, token: str) -> dict[str, Any] | None:
    """GET the supervisor's host metrics, or None on any failure — the sampler
    skips a tick rather than letting a supervisor blip kill the worker loop."""
    try:
        resp = await client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())
    except Exception:  # noqa: BLE001 - a missed sample is never worth a crash
        return None


async def sample_once(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    client: httpx.AsyncClient,
    token: str,
    *,
    tracker: RateTracker | None = None,
    now_s: float | None = None,
) -> bool:
    """Fetch the supervisor's host metrics and store one raw sample. Returns
    False (storing nothing) when the supervisor is unreachable — a missed tick.

    A `tracker` (the worker's long-lived one) derives network/disk throughput from
    the delta since the last successful sample; without it the rate columns store
    NULL. Only a successful fetch advances the tracker, so a missed tick doesn't
    corrupt the next interval's rate."""
    metrics = await fetch_supervisor_metrics(client, token)
    if metrics is None:
        return False
    rates = None
    if tracker is not None:
        rates = tracker.rates(time.monotonic() if now_s is None else now_s, metrics)
    await store_sample(maker, ctx, metrics, rates=rates)
    return True


async def store_sample(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    metrics: dict[str, Any],
    *,
    captured_at: datetime | None = None,
    rates: Mapping[str, float | None] | None = None,
) -> None:
    """Insert one raw sample from a supervisor `/metrics` payload. `rates` carries
    the derived network/disk throughput (bytes/sec); absent → the rate columns
    store NULL."""
    fan_rpm = metrics.get("fan_rpm")
    rates = rates or {}
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                """
                INSERT INTO app.host_metrics (
                    captured_at, mem_total_bytes, mem_available_bytes,
                    swap_total_bytes, swap_free_bytes, disk_total_bytes, disk_free_bytes,
                    load_1m, load_5m, load_15m, uptime_seconds, gpu_busy_percent,
                    power_w, fan_rpm_max, fan_rpm, containers,
                    net_rx_bps, net_tx_bps, disk_read_bps, disk_write_bps
                ) VALUES (
                    coalesce(:captured_at, now()), :mem_total, :mem_avail,
                    :swap_total, :swap_free, :disk_total, :disk_free,
                    :load_1m, :load_5m, :load_15m, :uptime, :gpu,
                    :power, :fan_max, cast(:fan_rpm AS jsonb), cast(:containers AS jsonb),
                    :net_rx_bps, :net_tx_bps, :disk_read_bps, :disk_write_bps
                )
                """
            ),
            {
                "captured_at": captured_at,
                "mem_total": metrics["mem_total_bytes"],
                "mem_avail": metrics["mem_available_bytes"],
                "swap_total": metrics["swap_total_bytes"],
                "swap_free": metrics["swap_free_bytes"],
                "disk_total": metrics["disk_total_bytes"],
                "disk_free": metrics["disk_free_bytes"],
                "load_1m": metrics["load_1m"],
                "load_5m": metrics["load_5m"],
                "load_15m": metrics["load_15m"],
                "uptime": metrics["uptime_seconds"],
                "gpu": metrics.get("gpu_busy_percent"),
                "power": metrics.get("apu_power_w"),
                "fan_max": fan_rpm_max_of(fan_rpm),
                "fan_rpm": json.dumps(fan_rpm) if fan_rpm is not None else None,
                "containers": json.dumps(metrics.get("containers"))
                if metrics.get("containers") is not None
                else None,
                "net_rx_bps": rates.get("net_rx_bps"),
                "net_tx_bps": rates.get("net_tx_bps"),
                "disk_read_bps": rates.get("disk_read_bps"),
                "disk_write_bps": rates.get("disk_write_bps"),
            },
        )


# The rollup aggregates raw samples into one row per clock hour. mem/swap/disk are
# stored as *used* (total - free) so the history reader is uniform across tables.
_ROLLUP_SQL = """
INSERT INTO app.host_metrics_hourly AS h (
    bucket, sample_count, load_1m_avg, load_1m_max, load_5m_avg, load_15m_avg,
    mem_total_bytes, mem_used_avg, mem_used_max, swap_used_avg, swap_used_max,
    disk_total_bytes, disk_used_avg, disk_used_max,
    gpu_busy_avg, gpu_busy_max, fan_rpm_avg, fan_rpm_max, power_w_avg, power_w_max,
    net_rx_bps_avg, net_rx_bps_max, net_tx_bps_avg, net_tx_bps_max,
    disk_read_bps_avg, disk_read_bps_max, disk_write_bps_avg, disk_write_bps_max
)
SELECT
    time_bucket(INTERVAL '1 hour', captured_at),
    count(*),
    avg(load_1m), max(load_1m), avg(load_5m), avg(load_15m),
    max(mem_total_bytes),
    avg(mem_total_bytes - mem_available_bytes)::bigint,
    max(mem_total_bytes - mem_available_bytes),
    avg(swap_total_bytes - swap_free_bytes)::bigint,
    max(swap_total_bytes - swap_free_bytes),
    max(disk_total_bytes),
    avg(disk_total_bytes - disk_free_bytes)::bigint,
    max(disk_total_bytes - disk_free_bytes),
    avg(gpu_busy_percent), max(gpu_busy_percent),
    avg(fan_rpm_max), max(fan_rpm_max),
    avg(power_w), max(power_w),
    avg(net_rx_bps), max(net_rx_bps), avg(net_tx_bps), max(net_tx_bps),
    avg(disk_read_bps), max(disk_read_bps), avg(disk_write_bps), max(disk_write_bps)
FROM app.host_metrics
WHERE captured_at >= :since
GROUP BY 1
ON CONFLICT (bucket) DO UPDATE SET
    sample_count = EXCLUDED.sample_count,
    load_1m_avg = EXCLUDED.load_1m_avg, load_1m_max = EXCLUDED.load_1m_max,
    load_5m_avg = EXCLUDED.load_5m_avg, load_15m_avg = EXCLUDED.load_15m_avg,
    mem_total_bytes = EXCLUDED.mem_total_bytes,
    mem_used_avg = EXCLUDED.mem_used_avg, mem_used_max = EXCLUDED.mem_used_max,
    swap_used_avg = EXCLUDED.swap_used_avg, swap_used_max = EXCLUDED.swap_used_max,
    disk_total_bytes = EXCLUDED.disk_total_bytes,
    disk_used_avg = EXCLUDED.disk_used_avg, disk_used_max = EXCLUDED.disk_used_max,
    gpu_busy_avg = EXCLUDED.gpu_busy_avg, gpu_busy_max = EXCLUDED.gpu_busy_max,
    fan_rpm_avg = EXCLUDED.fan_rpm_avg, fan_rpm_max = EXCLUDED.fan_rpm_max,
    power_w_avg = EXCLUDED.power_w_avg, power_w_max = EXCLUDED.power_w_max,
    net_rx_bps_avg = EXCLUDED.net_rx_bps_avg, net_rx_bps_max = EXCLUDED.net_rx_bps_max,
    net_tx_bps_avg = EXCLUDED.net_tx_bps_avg, net_tx_bps_max = EXCLUDED.net_tx_bps_max,
    disk_read_bps_avg = EXCLUDED.disk_read_bps_avg,
    disk_read_bps_max = EXCLUDED.disk_read_bps_max,
    disk_write_bps_avg = EXCLUDED.disk_write_bps_avg,
    disk_write_bps_max = EXCLUDED.disk_write_bps_max
"""


async def rollup(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    window: timedelta = ROLLUP_WINDOW,
    now: datetime | None = None,
) -> int:
    """Upsert hourly rollups for raw samples captured within `window`. Idempotent
    (ON CONFLICT refresh), so re-running keeps the current partial hour current.
    Returns the number of bucket rows written."""
    since = (now or _utcnow()) - window
    async with scoped_session(maker, ctx) as session:
        result = await session.execute(text(_ROLLUP_SQL), {"since": since})
        return cast("CursorResult[Any]", result).rowcount or 0


async def prune(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Delete raw samples past RAW_RETENTION and rollups past HOURLY_RETENTION.
    Returns `(raw_deleted, hourly_deleted)`. A time-ranged DELETE on the partition
    column lets Timescale exclude whole chunks, so this stays cheap."""
    moment = now or _utcnow()
    async with scoped_session(maker, ctx) as session:
        raw = await session.execute(
            text("DELETE FROM app.host_metrics WHERE captured_at < :cut"),
            {"cut": moment - RAW_RETENTION},
        )
        hourly = await session.execute(
            text("DELETE FROM app.host_metrics_hourly WHERE bucket < :cut"),
            {"cut": moment - HOURLY_RETENTION},
        )
    return (
        cast("CursorResult[Any]", raw).rowcount or 0,
        cast("CursorResult[Any]", hourly).rowcount or 0,
    )


# Raw and hourly share these output aliases so a point is built the same way from
# either source. Raw derives *used* on the fly; hourly reads the stored *_avg.
_RAW_SELECT = """
    avg(load_1m) AS load_1m, avg(load_5m) AS load_5m, avg(load_15m) AS load_15m,
    avg(mem_total_bytes - mem_available_bytes)::bigint AS mem_used,
    max(mem_total_bytes) AS mem_total,
    avg(swap_total_bytes - swap_free_bytes)::bigint AS swap_used,
    avg(disk_total_bytes - disk_free_bytes)::bigint AS disk_used,
    max(disk_total_bytes) AS disk_total,
    avg(gpu_busy_percent) AS gpu, max(fan_rpm_max) AS fan, avg(power_w) AS power,
    avg(net_rx_bps) AS net_rx, avg(net_tx_bps) AS net_tx,
    avg(disk_read_bps) AS disk_read, avg(disk_write_bps) AS disk_write
"""

_HOURLY_SELECT = """
    avg(load_1m_avg) AS load_1m, avg(load_5m_avg) AS load_5m, avg(load_15m_avg) AS load_15m,
    avg(mem_used_avg)::bigint AS mem_used, max(mem_total_bytes) AS mem_total,
    avg(swap_used_avg)::bigint AS swap_used,
    avg(disk_used_avg)::bigint AS disk_used, max(disk_total_bytes) AS disk_total,
    avg(gpu_busy_avg) AS gpu, max(fan_rpm_max) AS fan, avg(power_w_avg) AS power,
    avg(net_rx_bps_avg) AS net_rx, avg(net_tx_bps_avg) AS net_tx,
    avg(disk_read_bps_avg) AS disk_read, avg(disk_write_bps_avg) AS disk_write
"""


def _round(value: Any) -> float | None:
    return round(float(value), 3) if value is not None else None


async def history(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    since: datetime,
    until: datetime | None = None,
    max_points: int = MAX_POINTS,
) -> dict[str, Any]:
    """Downsampled host-metrics series for `[since, until)`.

    Reads raw 30s rows for spans up to RAW_QUERY_MAX, the hourly rollup beyond,
    and `time_bucket`s either to ~`max_points` so the chart payload stays small
    regardless of span. Every point carries the same keys (mem/swap/disk as
    *used* bytes plus their totals), so the frontend renders one shape."""
    until = until or _utcnow()
    span = max(until - since, timedelta(seconds=_SAMPLE_INTERVAL_SECONDS))
    raw = span <= RAW_QUERY_MAX
    table = "app.host_metrics" if raw else "app.host_metrics_hourly"
    time_col = "captured_at" if raw else "bucket"
    select_body = _RAW_SELECT if raw else _HOURLY_SELECT
    floor = _SAMPLE_INTERVAL_SECONDS if raw else 3600
    step = max(floor, int(span.total_seconds() // max(max_points, 1)))

    sql = f"""
        SELECT time_bucket(make_interval(secs => :step), {time_col}) AS t, {select_body}
        FROM {table}
        WHERE {time_col} >= :since AND {time_col} < :until
        GROUP BY t ORDER BY t
    """  # noqa: S608 - table/column names are module constants, only values bind
    async with scoped_session(maker, ctx) as session:
        rows = (
            (await session.execute(text(sql), {"step": step, "since": since, "until": until}))
            .mappings()
            .all()
        )

    def _int(value: Any) -> int | None:
        return int(value) if value is not None else None

    points = [
        {
            "t": r["t"].isoformat(),
            "load_1m": _round(r["load_1m"]),
            "load_5m": _round(r["load_5m"]),
            "load_15m": _round(r["load_15m"]),
            "mem_used_bytes": _int(r["mem_used"]),
            "mem_total_bytes": _int(r["mem_total"]),
            "swap_used_bytes": _int(r["swap_used"]),
            "disk_used_bytes": _int(r["disk_used"]),
            "disk_total_bytes": _int(r["disk_total"]),
            "gpu_busy_percent": _round(r["gpu"]),
            "fan_rpm_max": _int(r["fan"]),
            "power_w": _round(r["power"]),
            "net_rx_bps": _round(r["net_rx"]),
            "net_tx_bps": _round(r["net_tx"]),
            "disk_read_bps": _round(r["disk_read"]),
            "disk_write_bps": _round(r["disk_write"]),
        }
        for r in rows
    ]
    return {
        "resolution": "raw" if raw else "hourly",
        "step_seconds": step,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "points": points,
    }
