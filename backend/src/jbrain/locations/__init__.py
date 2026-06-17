"""Writing OwnTracks position reports to the `location_fixes` hypertable.

Runs under `device_context` (a non-owner, subject-pinned session), so the
location-fixes RLS subject pin is the barrier: a device can only insert fixes for
its own subject. Inserts are idempotent on the natural key so OwnTracks retries
(it resends the same fix until it gets a 200) never duplicate.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import device_context, scoped_session


@dataclass(frozen=True)
class LocationFix:
    """A normalized OwnTracks fix, ready to persist (SI units, server-trusted ts)."""

    captured_at: datetime
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    altitude_m: float | None = None
    velocity_mps: float | None = None
    course_deg: float | None = None
    battery_pct: int | None = None
    connection: str | None = None
    tracker_id: str | None = None
    raw: dict | None = None


class SqlLocationRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def ingest_fix(self, *, principal_id: str, subject_id: str, fix: LocationFix) -> bool:
        """Insert one fix for the device's subject; True if stored, False if a dup.

        The device session is pinned to `subject_id`, so RLS WITH CHECK rejects any
        attempt to write another subject's row (defense beyond the code stamp)."""
        async with scoped_session(self._maker, device_context(principal_id, subject_id)) as session:
            inserted = (
                await session.execute(
                    text(
                        "INSERT INTO app.location_fixes"
                        " (subject_id, principal_id, captured_at, latitude, longitude,"
                        "  accuracy_m, altitude_m, velocity_mps, course_deg, battery_pct,"
                        "  connection, tracker_id, raw)"
                        " VALUES (:sid, :pid, :captured_at, :lat, :lon,"
                        "  :acc, :alt, :vel, :cog, :batt, :conn, :tid, cast(:raw AS jsonb))"
                        " ON CONFLICT (subject_id, captured_at, latitude, longitude)"
                        " DO NOTHING RETURNING id"
                    ),
                    _params(principal_id, subject_id, fix),
                )
            ).first()
        return inserted is not None


def _params(principal_id: str, subject_id: str, fix: LocationFix) -> dict:
    import json

    return {
        "sid": subject_id,
        "pid": principal_id,
        "captured_at": fix.captured_at,
        "lat": fix.latitude,
        "lon": fix.longitude,
        "acc": fix.accuracy_m,
        "alt": fix.altitude_m,
        "vel": fix.velocity_mps,
        "cog": fix.course_deg,
        "batt": fix.battery_pct,
        "conn": fix.connection,
        "tid": fix.tracker_id,
        "raw": json.dumps(fix.raw) if fix.raw is not None else None,
    }
