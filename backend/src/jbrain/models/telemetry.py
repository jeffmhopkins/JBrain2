"""Operational telemetry tables — owner-only RLS, never domain data."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class LlmUsage(Base):
    """One adapter call's token usage (docs/reference/ANALYSIS.md "Token accounting").

    Written fire-and-forget by the usage recorder; costs are computed at query
    time from the config price table, so tokens here are the ground truth.
    """

    __tablename__ = "llm_usage"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeployHistory(Base):
    """One row per change in the version the running server was built from
    (migration 0160). The app appends a row on boot when its baked `git_sha`
    differs from the newest existing one, so this is a deduped timeline of what
    shipped and from when — enough to say which build produced any timestamped
    record. Owner-only RLS (a build revision is not domain data)."""

    __tablename__ = "deploy_history"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    git_sha: Mapped[str] = mapped_column(Text)
    git_describe: Mapped[str] = mapped_column(Text, default="unknown")
    build_time: Mapped[str] = mapped_column(Text, default="unknown")
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeployHistoryRepo:
    """Read/append the deploy history. The write is idempotent by design — recording
    the same running version twice (e.g. a plain restart onto the same image) adds no
    row, so the table stays a clean per-version timeline rather than a boot log."""

    async def record_if_changed(
        self, session: AsyncSession, *, git_sha: str, git_describe: str, build_time: str
    ) -> DeployHistory | None:
        """Append a row for this version unless the newest row already carries the same
        `git_sha`. Returns the new row, or None when nothing changed."""
        latest = (
            await session.execute(
                select(DeployHistory).order_by(DeployHistory.deployed_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if latest is not None and latest.git_sha == git_sha:
            return None
        row = DeployHistory(git_sha=git_sha, git_describe=git_describe, build_time=build_time)
        session.add(row)
        await session.flush()
        return row

    async def recent(self, session: AsyncSession, limit: int = 50) -> list[DeployHistory]:
        """The deploy history, newest first."""
        return list(
            (
                await session.execute(
                    select(DeployHistory).order_by(DeployHistory.deployed_at.desc()).limit(limit)
                )
            ).scalars()
        )


class HostMetric(Base):
    """One ~30s host-vitals sample (migration 0082). A Timescale hypertable on
    `captured_at`; the surrogate key is `(id, captured_at)` because a hypertable
    PK must include the partition column. Full fidelity: per-fan RPM and
    per-container memory ride along as jsonb, with `fan_rpm_max` lifted to a
    scalar so the hottest fan graphs/rolls up without unpacking jsonb. Raw rows
    are pruned at 30 days by the worker; `HostMetricHourly` keeps the long tail."""

    __tablename__ = "host_metrics"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    mem_total_bytes: Mapped[int] = mapped_column(BigInteger)
    mem_available_bytes: Mapped[int] = mapped_column(BigInteger)
    # What the box REALLY had, alongside what the kernel said it could have (migration 0168).
    # MemAvailable counts page cache as free; on this box that cache holds a second copy of
    # every model's weights (`--no-mmap`), so it read 29% used through a seven-hour livelock
    # that ended in a power cycle. These are the columns that make such an hour readable
    # afterwards: MemFree is the honest figure, Cached is the size of the lie, and the GTT
    # pair is where a loaded model actually sits. Nullable — rows predating 0168 have none.
    mem_free_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mem_cached_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mem_sreclaimable_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gtt_used_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gtt_total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vram_used_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    swap_total_bytes: Mapped[int] = mapped_column(BigInteger)
    swap_free_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_free_bytes: Mapped[int] = mapped_column(BigInteger)
    load_1m: Mapped[float] = mapped_column()
    load_5m: Mapped[float] = mapped_column()
    load_15m: Mapped[float] = mapped_column()
    uptime_seconds: Mapped[int] = mapped_column(BigInteger)
    gpu_busy_percent: Mapped[float | None] = mapped_column(nullable=True)
    # APU/SoC package watts (amdgpu power1_average); migration 0083, nullable.
    power_w: Mapped[float | None] = mapped_column(nullable=True)
    fan_rpm_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fan_rpm: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    containers: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Network + disk throughput in bytes/sec (migration 0131), derived by the
    # sampler from the delta between cumulative kernel counters. Nullable: the
    # first post-restart sample and any counter reset store NULL, not a spike.
    net_rx_bps: Mapped[float | None] = mapped_column(nullable=True)
    net_tx_bps: Mapped[float | None] = mapped_column(nullable=True)
    disk_read_bps: Mapped[float | None] = mapped_column(nullable=True)
    disk_write_bps: Mapped[float | None] = mapped_column(nullable=True)


class HostMetricHourly(Base):
    """One clock hour of `HostMetric` rolled up (migration 0082): avg + extreme
    of each scalar series, kept ~1 year. The partition column `bucket` is the
    natural key, so it is the PK. Populated and pruned app-side by the worker."""

    __tablename__ = "host_metrics_hourly"
    __table_args__ = {"schema": "app"}

    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    sample_count: Mapped[int] = mapped_column(Integer)
    load_1m_avg: Mapped[float] = mapped_column()
    load_1m_max: Mapped[float] = mapped_column()
    load_5m_avg: Mapped[float] = mapped_column()
    load_15m_avg: Mapped[float] = mapped_column()
    mem_total_bytes: Mapped[int] = mapped_column(BigInteger)
    mem_used_avg: Mapped[int] = mapped_column(BigInteger)
    mem_used_max: Mapped[int] = mapped_column(BigInteger)
    # The hour's WORST moment, not its average: a mean smooths away the spike worth finding.
    mem_free_min: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gtt_used_max: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    swap_used_avg: Mapped[int] = mapped_column(BigInteger)
    swap_used_max: Mapped[int] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_used_avg: Mapped[int] = mapped_column(BigInteger)
    disk_used_max: Mapped[int] = mapped_column(BigInteger)
    gpu_busy_avg: Mapped[float | None] = mapped_column(nullable=True)
    gpu_busy_max: Mapped[float | None] = mapped_column(nullable=True)
    fan_rpm_avg: Mapped[float | None] = mapped_column(nullable=True)
    fan_rpm_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    power_w_avg: Mapped[float | None] = mapped_column(nullable=True)
    power_w_max: Mapped[float | None] = mapped_column(nullable=True)
    # Network + disk throughput bytes/sec, avg + max per hour (migration 0131).
    net_rx_bps_avg: Mapped[float | None] = mapped_column(nullable=True)
    net_rx_bps_max: Mapped[float | None] = mapped_column(nullable=True)
    net_tx_bps_avg: Mapped[float | None] = mapped_column(nullable=True)
    net_tx_bps_max: Mapped[float | None] = mapped_column(nullable=True)
    disk_read_bps_avg: Mapped[float | None] = mapped_column(nullable=True)
    disk_read_bps_max: Mapped[float | None] = mapped_column(nullable=True)
    disk_write_bps_avg: Mapped[float | None] = mapped_column(nullable=True)
    disk_write_bps_max: Mapped[float | None] = mapped_column(nullable=True)


class BoxEvent(Base):
    """One notable thing the box did to the GPU (migration 0167) — a model load, an
    eviction, an image render — so the vitals graph can be read.

    Opened when the work starts and closed when it finishes: `ended_at` is NULL while it
    is still running, which is what lets the surface say "loading oss120b…" during the
    minute the trace is pinned rather than only explaining it afterwards. Written by
    `jbrain.box_events` from both the api and the worker; owner-only RLS, like the rest
    of this module. Pruned by the worker at `box_events.RETENTION`."""

    __tablename__ = "box_events"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    kind: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    # running | ok | failed. A row is opened `running` and settled by the span's exit;
    # one that never settles (the process died mid-load) stays `running` and is aged out
    # by the reader, which is honest — the load really did stop being observed.
    status: Mapped[str] = mapped_column(Text, default="ok")
    # Which process narrated it ("api" / "worker"), so a mystery load can be traced to
    # the half of the box that caused it.
    source: Mapped[str] = mapped_column(Text, default="")
