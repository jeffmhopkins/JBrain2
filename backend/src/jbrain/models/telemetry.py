"""Operational telemetry tables — owner-only RLS, never domain data."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
