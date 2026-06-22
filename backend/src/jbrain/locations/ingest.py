"""The shared OwnTracks location-ingest core.

Both transports feed this one function — the HTTP endpoint (`api/owntracks.py`)
and the MQTT consumer (`mqtt/consumer.py`). Parsing, the future-clock guard, the
idempotent store, and the inline geofence detection (L5a) live here once, under the
same subject-pinned `device_context`, so MQTT ingest can never drift from HTTP
ingest or skip geofencing (plan: "both transports share the one ingest core").
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.locations import LocationFix
from jbrain.locations.geofence import detect_transitions

if TYPE_CHECKING:
    from jbrain.push import PushNotifier

# A reported capture instant this far ahead of the server clock is bogus (a spoof
# or a wildly wrong device clock); drop it. Past fixes are kept (offline backfill).
MAX_FUTURE_SKEW = timedelta(hours=24)


class OwnTracksLocation(BaseModel):
    """The OwnTracks `_type:location` payload (extra keys tolerated and dropped)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    tst: int  # capture instant, Unix epoch seconds
    acc: float | None = Field(default=None, ge=0)
    alt: float | None = None
    vel: float | None = Field(default=None, ge=0)  # km/h per OwnTracks
    cog: float | None = None
    batt: int | None = None
    conn: str | None = None
    tid: str | None = None
    # JBrain360 extension (not OwnTracks): absolute linear-acceleration magnitude in
    # m/s² (gravity removed, low-pass filtered to a 0.2 s time constant on-device).
    accel: float | None = Field(default=None, ge=0)


class LocationSink(Protocol):
    async def ingest_fix(self, *, principal_id: str, subject_id: str, fix: LocationFix) -> bool: ...


def is_location_message(body: object) -> bool:
    """True for an OwnTracks `_type:location` body; transition / waypoints / lwt /
    anything else is not ours to store (server-side geofencing is authoritative)."""
    return isinstance(body, dict) and body.get("_type") == "location"


def to_fix(p: OwnTracksLocation) -> LocationFix:
    return LocationFix(
        captured_at=datetime.fromtimestamp(p.tst, UTC),
        latitude=p.lat,
        longitude=p.lon,
        accuracy_m=p.acc,
        altitude_m=p.alt,
        velocity_mps=p.vel / 3.6 if p.vel is not None else None,  # km/h -> m/s
        course_deg=p.cog,
        acceleration_mps2=p.accel,
        battery_pct=p.batt,
        connection=p.conn,
        tracker_id=p.tid,
        raw=p.model_dump(exclude_none=True),  # allowlisted to the declared fields
    )


async def ingest_location(
    sink: LocationSink,
    maker: "async_sessionmaker[AsyncSession]",
    *,
    principal_id: str,
    subject_id: str,
    body: dict[str, Any],
    notifier: "PushNotifier | None" = None,
) -> bool:
    """Validate + store one `_type:location` body for the device's subject, firing
    geofence detection only on a genuinely new fix.

    Raises `pydantic.ValidationError` on a schema-invalid body (the HTTP endpoint
    maps that to 422; the consumer logs and drops). Returns True iff a new fix was
    stored (False on an idempotent dup or a too-future fix). The subject is code-set
    by the caller from the authenticated principal, never the payload (L9).
    """
    payload = OwnTracksLocation.model_validate(body)
    fix = to_fix(payload)
    if fix.captured_at > datetime.now(UTC) + MAX_FUTURE_SKEW:
        return False
    inserted = await sink.ingest_fix(principal_id=principal_id, subject_id=subject_id, fix=fix)
    # Detect geofence crossings only on a new fix — a retry must not re-fire
    # transitions. detect_transitions is best-effort internally (never breaks ingest).
    if inserted:
        await detect_transitions(
            maker,
            principal_id=principal_id,
            subject_id=subject_id,
            captured_at=fix.captured_at,
            latitude=fix.latitude,
            longitude=fix.longitude,
            accuracy_m=fix.accuracy_m,
            notifier=notifier,
        )
    return inserted
