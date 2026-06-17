"""Phase 7 location tables (migrations 0060-0062).

The PostGIS `geography` columns (`location_fixes.geog`, `place_geofence.center`/
`polygon`) are deliberately NOT mapped here: `geog` is a DB-generated column and
all spatial predicates run as raw `ST_*` SQL on the RLS-scoped session, so the ORM
only carries the scalar columns the application reads/writes directly. This keeps
the models free of a PostGIS ORM dependency while the migrations own the geometry.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class LocationFix(Base):
    """One OwnTracks position report. A Timescale hypertable partitioned on
    `captured_at`; the surrogate key is composite `(id, captured_at)` because a
    hypertable PK must include the partition column."""

    __tablename__ = "location_fixes"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.subjects.id", ondelete="CASCADE")
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id"), nullable=True
    )
    domain_code: Mapped[str] = mapped_column(
        Text, ForeignKey("app.domains.code"), default="location"
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    latitude: Mapped[float] = mapped_column()
    longitude: Mapped[float] = mapped_column()
    accuracy_m: Mapped[float | None] = mapped_column(nullable=True)
    altitude_m: Mapped[float | None] = mapped_column(nullable=True)
    velocity_mps: Mapped[float | None] = mapped_column(nullable=True)
    course_deg: Mapped[float | None] = mapped_column(nullable=True)
    battery_pct: Mapped[int | None] = mapped_column(nullable=True)
    connection: Mapped[str | None] = mapped_column(Text, nullable=True)
    tracker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class PlaceGeofence(Base):
    """Derived spatial mirror of a Place's `geofence` predicate. Projected from the
    note-sourced graph; never edited directly (geometry columns are migration-owned
    and queried via raw `ST_*`)."""

    __tablename__ = "place_geofence"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    place_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.entities.id", ondelete="CASCADE")
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.subjects.id", ondelete="CASCADE"), nullable=True
    )
    domain_code: Mapped[str] = mapped_column(
        Text, ForeignKey("app.domains.code"), default="location"
    )
    name: Mapped[str] = mapped_column(Text, default="")
    radius_m: Mapped[float | None] = mapped_column(nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GeofenceState(Base):
    """Per-(subject, fence) hysteresis state the inline detector RMWs on each fix."""

    __tablename__ = "geofence_state"
    __table_args__ = {"schema": "app"}

    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.subjects.id", ondelete="CASCADE"), primary_key=True
    )
    place_geofence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.place_geofence.id", ondelete="CASCADE"),
        primary_key=True,
    )
    domain_code: Mapped[str] = mapped_column(
        Text, ForeignKey("app.domains.code"), default="location"
    )
    state: Mapped[str] = mapped_column(Text, default="unknown")
    confirming_fixes: Mapped[int] = mapped_column(default=0)
    since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fix_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
