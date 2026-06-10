"""Operational telemetry tables — owner-only RLS, never domain data."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class LlmUsage(Base):
    """One adapter call's token usage (docs/ANALYSIS.md "Token accounting").

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
