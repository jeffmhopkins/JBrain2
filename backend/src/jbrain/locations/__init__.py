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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, device_context, scoped_session
from jbrain.locations.access import LocationToolRefusal, require_full_owner

# `fixes_within` clamps to these so one aggregate query can never scan an
# unbounded window or sweep an absurd radius (a wide window is both a cost and a
# leak vector). 31 days covers "last month" answers; 50 km bounds a place query.
_FIXES_WITHIN_MAX_WINDOW = timedelta(days=31)
_FIXES_WITHIN_MAX_RADIUS_M = 50_000.0
_FIXES_WITHIN_MAX_LIMIT = 50_000

__all__ = [
    "LocationToolRefusal",
    "require_full_owner",
    "LocationFix",
    "DeviceActivity",
    "FixPoint",
    "NearestFix",
    "LatestPlace",
    "Dwell",
    "TimelineEntry",
    "PlaceGeofence",
    "NearbyPlace",
    "RosterEntry",
    "SqlLocationRepo",
]


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
class NearestFix:
    """The fix closest in time to a requested instant, with the signed-magnitude
    `gap_seconds` between them. Callers surface the gap so a stale fix is never
    reported as the subject's current position."""

    fix: FixPoint
    gap_seconds: float


@dataclass(frozen=True)
class LatestPlace:
    """The subject's current geofenced place, resolved to its Place entity's
    canonical name, and when they entered it."""

    place_entity_id: str
    place_name: str
    since: datetime | None


@dataclass(frozen=True)
class Dwell:
    """One enter→exit stay at a place: the interval the subject was inside its
    geofence. An open stay (no matching exit yet) is clamped to the query window's
    end so `seconds` is always a finite, non-negative duration."""

    place_entity_id: str
    place_name: str
    entered_at: datetime
    exited_at: datetime
    seconds: float


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


@dataclass(frozen=True)
class NearbyPlace:
    """A geofenced place within the queried radius of a center point, with the
    great-circle `distance_m` to that center. Carries the name and distance only —
    never the center or the fence coordinates (those stay server-side)."""

    place_entity_id: str
    name: str
    distance_m: float


@dataclass(frozen=True)
class RosterEntry:
    """One household subject's current presence: its display label, the place it is
    currently inside (None when outside every fence), and `last_seen` (its latest
    fix's time) so the caller can flag a stale fix and never report an old position
    as "here now"."""

    subject_id: str
    subject_label: str
    place_entity_id: str | None
    place_name: str | None
    since: datetime | None
    last_seen: datetime | None


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

    async def record_view(
        self,
        ctx: SessionContext,
        *,
        viewer_principal_id: str,
        viewer_subject_id: str,
        target_subject_id: str,
        path: str,
    ) -> None:
        """Append a who-saw-whom row for a location view (JBrain360 M3a). Runs under
        the viewer's ctx, so the view_audit WITH CHECK attributes the view correctly:
        the owner may write any row; a device only one about its own subject."""
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "INSERT INTO app.view_audit"
                    " (viewer_principal_id, viewer_subject_id, target_subject_id, path)"
                    " VALUES (:vp, :vs, :ts, :path)"
                ),
                {
                    "vp": viewer_principal_id or None,
                    "vs": viewer_subject_id or None,
                    "ts": target_subject_id,
                    "path": path,
                },
            )

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

    async def fixes_within(
        self,
        ctx: SessionContext,
        *,
        subject_id: str,
        since: datetime,
        until: datetime,
        center: tuple[float, float] | None = None,
        radius_m: float | None = None,
        limit: int,
    ) -> list[FixPoint]:
        """A device's fixes in `[since, until)`, oldest first, optionally restricted
        to those within `radius_m` of `center` ((lat, lon)). Powers `location_query`
        ("battery at <place> last night"): the caller passes a place's fence center +
        radius and aggregates the returned fixes.

        Reads `location_fixes`, which is STRICT RLS (full owner OR the device's own
        subject), so a narrowed session sees zero rows — RLS fails it closed, no
        application guard needed. The window is CLAMPED to a max span (a too-wide
        `since` is pulled forward) and the radius to a max, and `limit` is bounded,
        so one call can never scan the whole hypertable. The spatial predicate uses
        `ST_DWithin` over the GiST-indexed `geog` column; no coordinate crosses the
        boundary except inside the returned `FixPoint`s (render-only)."""
        # Clamp the window: pull `since` forward so the span never exceeds the max
        # (an unbounded "since the beginning of time" becomes the last N days).
        if until - since > _FIXES_WITHIN_MAX_WINDOW:
            since = until - _FIXES_WITHIN_MAX_WINDOW
        lim = max(1, min(_FIXES_WITHIN_MAX_LIMIT, limit))
        params: dict = {"sid": subject_id, "since": since, "until": until, "lim": lim}
        spatial = ""
        if center is not None and radius_m is not None:
            radius = max(1.0, min(_FIXES_WITHIN_MAX_RADIUS_M, radius_m))
            params.update({"lat": center[0], "lon": center[1], "radius": radius})
            spatial = (
                " AND ST_DWithin(geog,"
                " ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :radius)"
            )
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT captured_at, latitude, longitude, accuracy_m, battery_pct"
                        " FROM app.location_fixes"
                        " WHERE subject_id = cast(:sid AS uuid)"
                        "   AND captured_at >= :since AND captured_at < :until"
                        + spatial
                        + " ORDER BY captured_at LIMIT :lim"
                    ),
                    params,
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

    async def nearest_fix(
        self, ctx: SessionContext, *, subject_id: str, at: datetime, max_gap_seconds: float
    ) -> NearestFix | None:
        """The fix nearest in time to `at`, within `±max_gap_seconds`, or None.

        Reads `location_fixes`, which is STRICT RLS (full owner OR the device's own
        subject), so a narrowed session simply sees zero rows and gets None — no
        application guard is needed, RLS fails it closed. Returning the gap lets the
        caller refuse to report a far-off fix as the subject's current position. The
        nearest row on either side rides the `(subject_id, captured_at DESC)` index
        via two bounded one-row scans the planner merges."""
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT captured_at, latitude, longitude, accuracy_m, battery_pct,"
                        "   abs(extract(epoch FROM (captured_at - :at))) AS gap"
                        " FROM app.location_fixes"
                        " WHERE subject_id = cast(:sid AS uuid)"
                        "   AND captured_at >= :at - make_interval(secs => :gap)"
                        "   AND captured_at <= :at + make_interval(secs => :gap)"
                        " ORDER BY gap LIMIT 1"
                    ),
                    {"sid": subject_id, "at": at, "gap": max_gap_seconds},
                )
            ).first()
        if row is None:
            return None
        return NearestFix(
            fix=FixPoint(
                captured_at=row.captured_at,
                latitude=row.latitude,
                longitude=row.longitude,
                accuracy_m=row.accuracy_m,
                battery_pct=row.battery_pct,
            ),
            gap_seconds=float(row.gap),
        )

    async def latest_place(self, ctx: SessionContext, *, subject_id: str) -> LatestPlace | None:
        """The subject's CURRENT geofenced place (the one it is `inside`), resolved
        to its Place entity's canonical name, or None when it is not inside any.

        Reads `geofence_state` (STRICT RLS, same subject-pin as the fixes), so a
        narrowed session sees zero rows and gets None — RLS fails it closed. Joins
        through the geometry mirror to the Place entity for the name; the most
        recently entered inside-fence wins when more than one overlaps."""
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT pg.place_entity_id::text AS eid,"
                        "   COALESCE(ent.canonical_name, pg.name) AS name, gs.since"
                        " FROM app.geofence_state gs"
                        " JOIN app.place_geofence pg ON pg.id = gs.place_geofence_id"
                        " LEFT JOIN app.entities ent ON ent.id = pg.place_entity_id"
                        " WHERE gs.subject_id = cast(:sid AS uuid) AND gs.state = 'inside'"
                        " ORDER BY gs.since DESC NULLS LAST LIMIT 1"
                    ),
                    {"sid": subject_id},
                )
            ).first()
        if row is None:
            return None
        return LatestPlace(
            place_entity_id=row.eid, place_name=row.name or "a place", since=row.since
        )

    async def dwells(
        self,
        ctx: SessionContext,
        *,
        subject_id: str,
        place_entity_id: str | None = None,
        since: datetime,
        until: datetime,
    ) -> list[Dwell]:
        """The subject's enter→exit stays overlapping `[since, until)`, by pairing
        `location.geofence_transition` events in time order.

        Reads `app.events`, which is WEAK RLS (`has_domain_scope` only — a narrowed
        owner still sees these rows), so this MUST gate on `require_full_owner`
        first: RLS will NOT fail it closed. An enter still open at `until` is clamped
        to `until`; an exit with no preceding enter is dropped, as is any non-positive
        interval. `place_entity_id` optionally restricts to one place."""
        require_full_owner(ctx)
        clause = " AND e.payload->>'place_entity_id' = :eid" if place_entity_id else ""
        # No lower bound on the fetch on purpose: an enter that predates `since` is
        # needed to pair a stay already in progress at the window start. `_pair_dwells`
        # drops the fully-historical pairs (those that also exited before `since`).
        params: dict = {"sid": subject_id, "until": until}
        if place_entity_id:
            params["eid"] = place_entity_id
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT e.occurred_at,"
                        "   e.payload->>'transition' AS transition,"
                        "   e.payload->>'place_entity_id' AS eid,"
                        "   COALESCE(ent.canonical_name, 'a place') AS place_name"
                        " FROM app.events e"
                        " LEFT JOIN app.entities ent"
                        "   ON ent.id = cast(e.payload->>'place_entity_id' AS uuid)"
                        " WHERE e.type = 'location.geofence_transition'"
                        "   AND e.domain_code = 'location'"
                        "   AND e.payload->>'subject_id' = :sid"
                        "   AND e.occurred_at < :until" + clause + " ORDER BY e.occurred_at"
                    ),
                    params,
                )
            ).all()
        return _pair_dwells(rows, since=since, until=until)

    async def timeline(
        self, ctx: SessionContext, *, since: datetime, until: datetime, limit: int
    ) -> list[TimelineEntry]:
        """Geofence crossings in `[since, until)`, newest first, each resolved to its
        place's canonical name. Reads `app.events` (the location-domain transition
        the detector emits) joined to `app.entities` via the payload's
        `place_entity_id`. A crossing whose place entity was since deleted falls back
        to a generic label rather than vanishing from the audit.

        `app.events` is WEAK RLS (`has_domain_scope` only), so the full-owner gate
        here is the real barrier — a narrowed owner is refused, not handed rows."""
        require_full_owner(ctx)
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
        self-rendered map can project them without a geometry lib client-side.

        `place_geofence` READ is WEAK RLS (`has_domain_scope`, also satisfied by a
        device key), so the full-owner gate here is the real barrier."""
        require_full_owner(ctx)
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

    async def nearby(
        self,
        ctx: SessionContext,
        *,
        subject_id: str | None = None,
        center: tuple[float, float] | None = None,
        radius_m: float,
        limit: int,
    ) -> list[NearbyPlace]:
        """Geofenced places within `radius_m` of a center, nearest first (name +
        distance only). The center is either an explicit `(lat, lon)` or, when only
        `subject_id` is given, the subject's most recent fix — resolved INSIDE the
        query so a coordinate never crosses the repo boundary into model-facing text.

        Reads `place_geofence` (the fence names/geometry), which is WEAK RLS
        (`has_domain_scope`, also satisfied by a device key and by a narrowed owner),
        so this MUST gate on `require_full_owner` first — RLS will NOT fail it closed,
        and the distance to a private fence is itself a leak. Bounded by `ST_DWithin`
        and ordered by the `<->` KNN operator over the `place_geofence_center_idx`
        GiST index; circular fences only (a fence stores center XOR polygon)."""
        require_full_owner(ctx)
        if center is None and subject_id is None:
            return []
        if center is not None:
            origin = "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography"
            params: dict = {"lon": center[1], "lat": center[0]}
        else:
            # The subject's latest fix as the origin — subselected so no coordinate is
            # ever returned to the caller; only the resulting distances are.
            origin = (
                "(SELECT geog FROM app.location_fixes"
                "  WHERE subject_id = cast(:sid AS uuid)"
                "  ORDER BY captured_at DESC LIMIT 1)"
            )
            params = {"sid": subject_id}
        params.update({"radius": radius_m, "lim": limit})
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "WITH o AS (SELECT " + origin + " AS g)"
                        " SELECT pg.place_entity_id::text AS eid,"
                        "   COALESCE(ent.canonical_name, pg.name) AS name,"
                        "   ST_Distance(pg.center, o.g) AS distance_m"
                        " FROM app.place_geofence pg"
                        " CROSS JOIN o"
                        " LEFT JOIN app.entities ent ON ent.id = pg.place_entity_id"
                        " WHERE o.g IS NOT NULL AND pg.center IS NOT NULL AND pg.enabled"
                        "   AND ST_DWithin(pg.center, o.g, :radius)"
                        " ORDER BY pg.center <-> o.g LIMIT :lim"
                    ),
                    params,
                )
            ).all()
        return [
            NearbyPlace(
                place_entity_id=r.eid,
                name=r.name or "a place",
                distance_m=float(r.distance_m),
            )
            for r in rows
        ]

    async def home_roster(self, ctx: SessionContext) -> list[RosterEntry]:
        """Every device subject's current place + last-seen freshness, for the
        household presence read. Resolves place NAMES through `place_geofence`/
        `entities` (WEAK RLS), so it MUST gate on `require_full_owner` first — a
        narrowed owner is refused, not handed presence rows. The current inside-fence
        (most recently entered wins) is left-joined so an outside subject still
        appears (place None); `last_seen` is its latest fix so the caller can flag a
        stale fix rather than report an old position as "here now"."""
        require_full_owner(ctx)
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "WITH inside AS ("
                        "  SELECT DISTINCT ON (gs.subject_id) gs.subject_id,"
                        "    pg.place_entity_id,"
                        "    COALESCE(ent.canonical_name, pg.name) AS place_name, gs.since"
                        "  FROM app.geofence_state gs"
                        "  JOIN app.place_geofence pg ON pg.id = gs.place_geofence_id"
                        "  LEFT JOIN app.entities ent ON ent.id = pg.place_entity_id"
                        "  WHERE gs.state = 'inside'"
                        "  ORDER BY gs.subject_id, gs.since DESC NULLS LAST"
                        "), seen AS ("
                        "  SELECT DISTINCT ON (subject_id) subject_id, captured_at"
                        "  FROM app.location_fixes ORDER BY subject_id, captured_at DESC"
                        ")"
                        " SELECT s.id::text AS sid, s.display_name AS label,"
                        "   i.place_entity_id::text AS eid, i.place_name, i.since,"
                        "   seen.captured_at AS last_seen"
                        " FROM app.subjects s"
                        " LEFT JOIN inside i ON i.subject_id = s.id"
                        " LEFT JOIN seen ON seen.subject_id = s.id"
                        " WHERE s.kind = 'device'"
                        "   AND (i.subject_id IS NOT NULL OR seen.subject_id IS NOT NULL)"
                        " ORDER BY s.display_name"
                    )
                )
            ).all()
        return [
            RosterEntry(
                subject_id=r.sid,
                subject_label=r.label or "a device",
                place_entity_id=r.eid,
                place_name=r.place_name,
                since=r.since,
                last_seen=r.last_seen,
            )
            for r in rows
        ]


def _pair_dwells(rows: Sequence[Any], *, since: datetime, until: datetime) -> list[Dwell]:
    """Fold time-ordered transition rows into enter→exit Dwells, per place.

    A second `enter` for a place already open re-opens at the earlier enter (a
    duplicate/redundant enter never shortens the stay); an `exit` with no open
    enter for its place is dropped (an orphan); an enter still open at the end of
    the rows is clamped to `until`. Non-positive intervals (an exit at or before
    its enter) are discarded — they describe no real stay. A stay that ended at or
    before `since` is dropped: the fetch has no lower bound (to pair in-progress
    stays), so the window's low edge is enforced here on the paired result."""
    open_enter: dict[str, tuple[datetime, str]] = {}
    dwells: list[Dwell] = []

    def _emit(eid: str, name: str, entered: datetime, exited: datetime) -> None:
        seconds = (exited - entered).total_seconds()
        if seconds > 0 and exited > since:
            dwells.append(Dwell(eid, name, entered, exited, seconds))

    for row in rows:
        eid = row.eid
        if eid is None:
            continue
        if row.transition == "enter":
            if eid not in open_enter:
                open_enter[eid] = (row.occurred_at, row.place_name)
        elif row.transition == "exit":
            opened = open_enter.pop(eid, None)
            if opened is not None:
                _emit(eid, opened[1], opened[0], row.occurred_at)
    for eid, (entered, name) in open_enter.items():
        _emit(eid, name, entered, until)
    dwells.sort(key=lambda d: d.entered_at)
    return dwells


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
