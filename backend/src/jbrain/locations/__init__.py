"""Writing OwnTracks position reports to the `location_fixes` hypertable.

Runs under `device_context` (a non-owner, subject-pinned session), so the
location-fixes RLS subject pin is the barrier: a device can only insert fixes for
its own subject. Inserts are idempotent on the natural key so OwnTracks retries
(it resends the same fix until it gets a 200) never duplicate.

The READ side (Phase 7 Wave 5) is the opposite: it runs under the caller's *full
owner* `SessionContext`, where `app.is_full_owner()` lets the owner see every
device's track. Those reads are the only place location data leaves the box, and
only ever to the owner — the UI's Devices / Timeline / Map tabs read here.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, device_context, scoped_session


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


@dataclass(frozen=True)
class DeviceActivity:
    """Per-device location aggregates for the Devices tab: when the device was last
    heard from and the battery/connection it reported then, plus its total fix
    count. Keyed by the device's subject id."""

    subject_id: str
    last_seen: datetime | None
    battery_pct: int | None
    connection: str | None
    fix_count: int


@dataclass(frozen=True)
class FixPoint:
    """One stored fix, trimmed to what the map renders (raw doubles, never the
    `raw` jsonb or the SSID-revealing metadata)."""

    captured_at: datetime
    latitude: float
    longitude: float
    accuracy_m: float | None
    battery_pct: int | None


@dataclass(frozen=True)
class TimelineEntry:
    """One geofence crossing for the Timeline feed, with the place resolved to its
    canonical name (the feed reads as "left/arrived at <place>")."""

    occurred_at: datetime
    subject_id: str
    transition: str  # 'enter' | 'exit'
    place_entity_id: str
    place_name: str


@dataclass(frozen=True)
class PlaceGeofence:
    """A geofenced place for the map overlay — the derived mirror's geometry, named
    from its Place entity. A circle carries `center` + `radius_m`; a polygon carries
    its `polygon` ring as [lat, lon] pairs (the other half is None)."""

    place_entity_id: str
    name: str
    enabled: bool
    center: tuple[float, float] | None  # (lat, lon)
    radius_m: float | None
    polygon: list[tuple[float, float]] | None  # ring of (lat, lon)


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

    async def device_activity(self, ctx: SessionContext) -> dict[str, DeviceActivity]:
        """Per-device last-seen + latest battery/connection + total fix count, keyed
        by subject id. Runs under the owner ctx, so RLS shows every device's rows; a
        device with no fixes yet simply has no entry. The latest row per subject
        rides the `(subject_id, captured_at DESC)` index via DISTINCT ON."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "WITH latest AS ("
                        "  SELECT DISTINCT ON (subject_id) subject_id, captured_at,"
                        "    battery_pct, connection"
                        "  FROM app.location_fixes ORDER BY subject_id, captured_at DESC"
                        "), counts AS ("
                        "  SELECT subject_id, count(*) AS fix_count"
                        "  FROM app.location_fixes GROUP BY subject_id"
                        ")"
                        " SELECT l.subject_id::text AS sid, l.captured_at, l.battery_pct,"
                        "   l.connection, c.fix_count"
                        " FROM latest l JOIN counts c ON c.subject_id = l.subject_id"
                    )
                )
            ).all()
        return {
            r.sid: DeviceActivity(
                subject_id=r.sid,
                last_seen=r.captured_at,
                battery_pct=r.battery_pct,
                connection=r.connection,
                fix_count=r.fix_count,
            )
            for r in rows
        }

    async def fixes(
        self,
        ctx: SessionContext,
        *,
        subject_id: str,
        since: datetime,
        until: datetime,
        limit: int,
    ) -> list[FixPoint]:
        """A device's fixes in `[since, until)`, oldest first (a drawable trail), via
        the `(subject_id, captured_at DESC)` index. `limit` bounds an over-wide
        window so one request can never stream the whole hypertable; the owner ctx +
        RLS still scope the rows. The map's Trail/Heat modes read here."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT captured_at, latitude, longitude, accuracy_m, battery_pct"
                        " FROM app.location_fixes"
                        " WHERE subject_id = cast(:sid AS uuid)"
                        "   AND captured_at >= :since AND captured_at < :until"
                        " ORDER BY captured_at LIMIT :lim"
                    ),
                    {"sid": subject_id, "since": since, "until": until, "lim": limit},
                )
            ).all()
        return [
            FixPoint(
                captured_at=r.captured_at,
                latitude=r.latitude,
                longitude=r.longitude,
                accuracy_m=r.accuracy_m,
                battery_pct=r.battery_pct,
            )
            for r in rows
        ]

    async def timeline(
        self, ctx: SessionContext, *, since: datetime, until: datetime, limit: int
    ) -> list[TimelineEntry]:
        """Geofence crossings in `[since, until)`, newest first, each resolved to its
        place's canonical name. Reads `app.events` (the location-domain transition
        the detector emits) joined to `app.entities` via the payload's
        `place_entity_id`. A crossing whose place entity was since deleted falls back
        to a generic label rather than vanishing from the audit."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT e.occurred_at,"
                        "   e.payload->>'subject_id' AS sid,"
                        "   e.payload->>'transition' AS transition,"
                        "   e.payload->>'place_entity_id' AS eid,"
                        "   ent.canonical_name AS place_name"
                        " FROM app.events e"
                        " LEFT JOIN app.entities ent"
                        "   ON ent.id = cast(e.payload->>'place_entity_id' AS uuid)"
                        " WHERE e.type = 'location.geofence_transition'"
                        "   AND e.domain_code = 'location'"
                        "   AND e.occurred_at >= :since AND e.occurred_at < :until"
                        " ORDER BY e.occurred_at DESC LIMIT :lim"
                    ),
                    {"since": since, "until": until, "lim": limit},
                )
            ).all()
        return [
            TimelineEntry(
                occurred_at=r.occurred_at,
                subject_id=r.sid,
                transition=r.transition,
                place_entity_id=r.eid,
                place_name=r.place_name or "a place",
            )
            for r in rows
        ]

    async def places(self, ctx: SessionContext) -> list[PlaceGeofence]:
        """Every geofenced place's geometry for the map overlay, named from its
        Place entity. Reads the derived `place_geofence` mirror (the graph stays the
        source of truth, #7); center/polygon come back as lat/lon via PostGIS so the
        self-rendered map can project them without a geometry lib client-side."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT pg.place_entity_id::text AS eid,"
                        "   COALESCE(ent.canonical_name, pg.name) AS name, pg.enabled,"
                        "   pg.radius_m,"
                        "   ST_Y(pg.center::geometry) AS lat, ST_X(pg.center::geometry) AS lon,"
                        "   ST_AsGeoJSON(pg.polygon::geometry) AS polygon_geojson"
                        " FROM app.place_geofence pg"
                        " LEFT JOIN app.entities ent ON ent.id = pg.place_entity_id"
                        " ORDER BY name"
                    )
                )
            ).all()
        return [
            PlaceGeofence(
                place_entity_id=r.eid,
                name=r.name or "Place",
                enabled=r.enabled,
                center=(r.lat, r.lon) if r.lat is not None and r.lon is not None else None,
                radius_m=r.radius_m,
                polygon=_polygon_ring(r.polygon_geojson),
            )
            for r in rows
        ]


def _polygon_ring(geojson: str | None) -> list[tuple[float, float]] | None:
    """The outer ring of a PostGIS `ST_AsGeoJSON` Polygon as [lat, lon] pairs (it
    encodes [lon, lat]); None when there is no polygon."""
    if not geojson:
        return None
    import json

    coords = json.loads(geojson).get("coordinates")
    if not coords:
        return None
    return [(lat, lon) for lon, lat in coords[0]]


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
