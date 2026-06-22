"""Location API: the owner's read-only view of the location slice (Phase 7 Wave 5).

Owner-only and read-only. The phones write fixes through `/owntracks` (a device
session); this surface is where the owner — and only the full owner — reads them
back, for the PWA's three tabs:

- **Devices** — each provisioned device with its last-seen, battery, connection,
  and fix count (identity from the device repo, activity from the fix stream).
- **Timeline** — the geofence-crossing feed ("left / arrived at <place>").
- **Map** — a device's fixes between two timestamps (the Trail / Heat modes).

RLS is the real barrier: every query runs under the owner's full `SessionContext`,
so `app.is_full_owner()` admits the rows and a narrowed/agent session would see
none. The `owner_only` router dependency 403s a non-owner before the DB. Fixes are
trimmed to coordinates + accuracy + battery — never the `raw` jsonb or the
SSID/BSSID metadata, which is location-revealing and stays server-side.
"""

from datetime import UTC, datetime, timedelta
from typing import cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jbrain.api.deps import PrincipalDep, owner_only
from jbrain.api.notes import ctx_for
from jbrain.api.settings import get_settings_store
from jbrain.citygeocode import CityGeocoder
from jbrain.devices.repo import DeviceInfo, DeviceRepo, SqlDeviceRepo
from jbrain.locations import (
    DeviceActivity,
    Dwell,
    FixPoint,
    PlaceGeofence,
    SqlLocationRepo,
    TimelineEntry,
)
from jbrain.locations.digest import DayTrack, Digest, PlaceSeen, PlaceSegment, Trip, compute_digest
from jbrain.locations.presence import Presence, read_owner_presence

router = APIRouter(prefix="/locations", dependencies=[Depends(owner_only)])

_DEFAULT_TIMELINE_WINDOW = timedelta(days=30)
_DEFAULT_FIXES_WINDOW = timedelta(days=1)
# Bound a single read so an over-wide window can never stream the whole hypertable
# (the owner can page by narrowing the date range).
_FIXES_LIMIT = 20_000
_TIMELINE_LIMIT = 500
# The digest windows: the weekly view (the default) covers the trailing 7 local days;
# the nightly view the trailing 24h (the owner toggles between them). Compute-on-read,
# so these are read-time spans, not a stored cadence.
_DIGEST_WEEK = timedelta(days=7)
_DIGEST_NIGHT = timedelta(days=1)
# The home anchor is resolved by name (case-insensitive), exactly as the dwell tools
# resolve nights-away; a saved place named any of these counts as "home".
_HOME_NAMES = ("home",)


def get_location_repo(request: Request) -> SqlLocationRepo:
    return cast(SqlLocationRepo, request.app.state.location_repo)


def get_device_repo(request: Request) -> DeviceRepo:
    return cast(DeviceRepo, request.app.state.device_repo)


def get_sql_device_repo(request: Request) -> SqlDeviceRepo:
    return cast(SqlDeviceRepo, request.app.state.device_repo)


def get_city_geocoder(request: Request) -> CityGeocoder:
    return cast(CityGeocoder, request.app.state.city_geocoder)


log = structlog.get_logger()


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid timestamp: {ts!r}") from exc


class DeviceSummaryOut(BaseModel):
    """A provisioned device for the Devices tab: stable identity + its current
    activity. `revoked` means it has no active key; `last_seen` is None until the
    device's first fix lands."""

    id: str
    label: str
    created_at: str
    revoked: bool
    last_seen: str | None
    battery_pct: int | None
    connection: str | None
    velocity_mps: float | None
    fix_count: int

    @classmethod
    def of(cls, d: DeviceInfo, a: DeviceActivity | None) -> "DeviceSummaryOut":
        return cls(
            id=d.id,
            label=d.label,
            created_at=d.created_at.isoformat(),
            revoked=d.revoked,
            last_seen=a.last_seen.isoformat() if a and a.last_seen else None,
            battery_pct=a.battery_pct if a else None,
            connection=a.connection if a else None,
            velocity_mps=a.velocity_mps if a else None,
            fix_count=a.fix_count if a else 0,
        )


class FixPointOut(BaseModel):
    captured_at: str
    latitude: float
    longitude: float
    accuracy_m: float | None
    battery_pct: int | None

    @classmethod
    def of(cls, f: FixPoint) -> "FixPointOut":
        return cls(
            captured_at=f.captured_at.isoformat(),
            latitude=f.latitude,
            longitude=f.longitude,
            accuracy_m=f.accuracy_m,
            battery_pct=f.battery_pct,
        )


class TimelineEntryOut(BaseModel):
    occurred_at: str
    subject_id: str
    transition: str
    place_entity_id: str
    place_name: str

    @classmethod
    def of(cls, e: TimelineEntry) -> "TimelineEntryOut":
        return cls(
            occurred_at=e.occurred_at.isoformat(),
            subject_id=e.subject_id,
            transition=e.transition,
            place_entity_id=e.place_entity_id,
            place_name=e.place_name,
        )


class LatLon(BaseModel):
    lat: float
    lon: float


class PlaceOut(BaseModel):
    """A geofenced place for the map overlay: a circle (center + radius_m) or a
    polygon (ring of points). The geometry is the derived mirror; the graph stays
    the source of truth."""

    place_entity_id: str
    name: str
    enabled: bool
    center: LatLon | None
    radius_m: float | None
    polygon: list[LatLon] | None

    @classmethod
    def of(cls, p: PlaceGeofence) -> "PlaceOut":
        return cls(
            place_entity_id=p.place_entity_id,
            name=p.name,
            enabled=p.enabled,
            center=LatLon(lat=p.center[0], lon=p.center[1]) if p.center else None,
            radius_m=p.radius_m,
            polygon=[LatLon(lat=lat, lon=lon) for lat, lon in p.polygon] if p.polygon else None,
        )


@router.get("/devices")
async def list_devices(request: Request, principal: PrincipalDep) -> list[DeviceSummaryOut]:
    """Every provisioned device with its last-seen / battery / connection / fix
    count. Identity comes from the device repo (so a freshly provisioned device with
    no fixes still appears); activity is merged in per subject id."""
    ctx = ctx_for(principal)
    devices = await get_device_repo(request).list(ctx)
    activity = await get_location_repo(request).device_activity(ctx)
    return [DeviceSummaryOut.of(d, activity.get(d.id)) for d in devices]


@router.get("/fixes")
async def list_fixes(
    request: Request,
    principal: PrincipalDep,
    subject_id: str,
    since: str | None = None,
    until: str | None = None,
) -> list[FixPointOut]:
    """A device's fixes in `[since, until)`, oldest first — the drawable trail the
    map's Trail/Heat modes render. The window defaults to the last day."""
    end = _parse(until) or datetime.now(UTC)
    start = _parse(since) or (end - _DEFAULT_FIXES_WINDOW)
    repo = get_location_repo(request)
    ctx = ctx_for(principal)
    rows = await repo.fixes(ctx, subject_id=subject_id, since=start, until=end, limit=_FIXES_LIMIT)
    # Who-saw-whom: record that this viewer read this subject's track (M3a). The
    # audit is an append-only side-effect; a write failure must not break the read.
    try:
        await repo.record_view(
            ctx,
            viewer_principal_id=principal.id,
            viewer_subject_id=principal.subject_id,
            target_subject_id=subject_id,
            path="history",
        )
    except Exception as exc:  # noqa: BLE001 - audit failure is logged, never a 500
        log.warning("locations.audit_failed", error=repr(exc))
    return [FixPointOut.of(f) for f in rows]


@router.get("/timeline")
async def list_timeline(
    request: Request,
    principal: PrincipalDep,
    since: str | None = None,
    until: str | None = None,
) -> list[TimelineEntryOut]:
    """The geofence-crossing feed in `[since, until)`, newest first, each resolved
    to its place name. The window defaults to the last 30 days."""
    end = _parse(until) or datetime.now(UTC)
    start = _parse(since) or (end - _DEFAULT_TIMELINE_WINDOW)
    rows = await get_location_repo(request).timeline(
        ctx_for(principal), since=start, until=end, limit=_TIMELINE_LIMIT
    )
    return [TimelineEntryOut.of(e) for e in rows]


@router.get("/places")
async def list_places(request: Request, principal: PrincipalDep) -> list[PlaceOut]:
    """Every geofenced place's geometry, for the map's fence overlay. The geometry
    is the derived mirror; the geofence editor edits a place note, never this."""
    rows = await get_location_repo(request).places(ctx_for(principal))
    return [PlaceOut.of(p) for p in rows]


class ShareRequest(BaseModel):
    shared: bool


@router.put("/places/{place_entity_id}/share", status_code=204)
async def set_place_share(
    request: Request, principal: PrincipalDep, place_entity_id: str, body: ShareRequest
) -> None:
    """Toggle whether a geofenced place is shared with family members (M4c). Owner
    only (the router gate + the `place_share` write RLS). Sharing surfaces the
    place's name + fence in members' overlay/timeline; un-sharing removes it."""
    await get_location_repo(request).set_place_shared(
        ctx_for(principal), place_entity_id=place_entity_id, shared=body.shared
    )


class AddressOut(BaseModel):
    address: str | None


@router.get("/geocode")
async def reverse_geocode(request: Request, lat: float, lon: float) -> AddressOut:
    """A coordinate's nearest-city name from the on-box offline geocoder, for the map
    caption. Local-only (no egress); fails closed to `None` on any error so the UI
    simply shows no caption. City-level, not a street address."""
    try:
        hit = get_city_geocoder(request).nearest(lat, lon)
    except Exception as exc:  # noqa: BLE001 - a geocoder hiccup is a missing caption, not a 500
        log.warning("locations.geocode_failed", error=repr(exc))
        return AddressOut(address=None)
    return AddressOut(address=hit.label if hit else None)


# ===== L7a — the nightly/weekly place digest (compute-on-read) =====
# No table, no migration, no scheduler: the rollup is computed at request time from
# the owner's own device dwells (`app.events`, WEAK RLS). `app.events`/`place_geofence`
# are NOT fail-closed by RLS for a narrowed owner, so the route's owner_only gate +
# the repo's `require_full_owner` are the real barrier here, asserted in the tests.


class PlaceSegmentOut(BaseModel):
    """One bar on a day's place-track: a place name (None = a no-signal gap) and the
    fraction of the local day it spans. Names + times only — no coordinate."""

    place_name: str | None
    start: float
    width: float
    entered_at: str
    exited_at: str

    @classmethod
    def of(cls, s: PlaceSegment) -> "PlaceSegmentOut":
        return cls(
            place_name=s.place_name,
            start=s.start,
            width=s.width,
            entered_at=s.entered_at.isoformat(),
            exited_at=s.exited_at.isoformat(),
        )


class DayTrackOut(BaseModel):
    day: str
    segments: list[PlaceSegmentOut]
    home: bool
    has_data: bool

    @classmethod
    def of(cls, d: DayTrack) -> "DayTrackOut":
        return cls(
            day=d.day.isoformat(),
            segments=[PlaceSegmentOut.of(s) for s in d.segments],
            home=d.home,
            has_data=d.has_data,
        )


class PlaceSeenOut(BaseModel):
    place_name: str
    first_seen: str
    last_seen: str

    @classmethod
    def of(cls, p: PlaceSeen) -> "PlaceSeenOut":
        return cls(
            place_name=p.place_name,
            first_seen=p.first_seen.isoformat(),
            last_seen=p.last_seen.isoformat(),
        )


class TripOut(BaseModel):
    place_name: str
    day: str
    entered_at: str
    exited_at: str
    seconds: float

    @classmethod
    def of(cls, t: Trip) -> "TripOut":
        return cls(
            place_name=t.place_name,
            day=t.day.isoformat(),
            entered_at=t.entered_at.isoformat(),
            exited_at=t.exited_at.isoformat(),
            seconds=t.seconds,
        )


class DigestOut(BaseModel):
    """The computed place digest for the period — per-day tracks + headline rollups.
    Coordinate-free by construction (every field is a name, a time, or a count)."""

    period: str
    since: str
    until: str
    timezone: str
    days: list[DayTrackOut]
    nights_home: int
    nights_total: int
    places_visited: int
    longest_trip: TripOut | None
    seen: list[PlaceSeenOut]
    computed_at: str

    @classmethod
    def of(cls, d: Digest, *, computed_at: datetime) -> "DigestOut":
        return cls(
            period=d.period,
            since=d.since.isoformat(),
            until=d.until.isoformat(),
            timezone=d.timezone,
            days=[DayTrackOut.of(t) for t in d.days],
            nights_home=d.nights_home,
            nights_total=d.nights_total,
            places_visited=d.places_visited,
            longest_trip=TripOut.of(d.longest_trip) if d.longest_trip is not None else None,
            seen=[PlaceSeenOut.of(s) for s in d.seen],
            computed_at=computed_at.isoformat(),
        )


def _home_name(places: list[PlaceGeofence]) -> str | None:
    """The saved place that counts as "home" (case-insensitive name match), or None
    when none is saved — resolved by name exactly as the dwell tools resolve it."""
    for p in places:
        if p.name.casefold() in _HOME_NAMES:
            return p.name
    return None


async def _owner_dwells(
    repo: SqlLocationRepo,
    devices: SqlDeviceRepo,
    ctx,
    *,
    since: datetime,
    until: datetime,  # noqa: ANN001
) -> list[Dwell]:
    """Every paired stay for the owner's OWN devices in `[since, until)`, merged across
    devices (the owner may carry more than one). `dwells` is full-owner gated (it reads
    WEAK-RLS `app.events`); resolving the owner's device subjects is the deterministic
    "Me" hard-link, never an LLM/fuzzy match."""
    subs = await devices.owner_device_subjects(ctx)
    merged: list[Dwell] = []
    for sid in subs:
        merged.extend(await repo.dwells(ctx, subject_id=sid, since=since, until=until))
    return merged


@router.get("/digest")
async def place_digest(
    request: Request, principal: PrincipalDep, period: str = "week"
) -> DigestOut:
    """The owner's place digest for the period (week default, or night). Computed on
    read from the owner's own dwells — there is no stored feed and no scheduled write.

    Owner-only and FULL-owner gated: `app.events`/`place_geofence` are WEAK RLS (a
    narrowed owner still passes them), so the route's `owner_only` dependency + the
    repo's `require_full_owner` are the barrier — RLS will not fail this closed. A
    narrowed/non-owner session never reaches here (403/refused). Names + times only."""
    if period not in ("week", "night"):
        raise HTTPException(status_code=400, detail="period must be 'week' or 'night'")
    ctx = ctx_for(principal)
    repo = get_location_repo(request)
    devices = get_sql_device_repo(request)
    now = datetime.now(UTC)
    since = now - (_DIGEST_NIGHT if period == "night" else _DIGEST_WEEK)
    # `places()` is full-owner gated and reads place_geofence — it both names "home"
    # and is the first weak-table read the gate must pass before any dwell is fetched.
    places = await repo.places(ctx)
    dwells = await _owner_dwells(repo, devices, ctx, since=since, until=now)
    tz = await get_settings_store(request).owner_timezone(ctx)
    digest = compute_digest(
        dwells,
        since=since,
        until=now,
        timezone=tz,
        home_name=_home_name(places),
        period=period,
        computed_at=now,
    )
    return DigestOut.of(digest, computed_at=now)


# ===== L7b — the app-open presence read (the owner's own latest place) =====


class PresenceOut(BaseModel):
    """The owner's own current/last-known place + freshness for the app-open toast.
    `present` False → no usable fix (the toast is absent); `stale` flips it to the
    amber "last known" tone. Names + times only — no coordinate."""

    present: bool
    place_name: str | None
    last_seen: str | None
    age_seconds: float | None
    stale: bool

    @classmethod
    def of(cls, p: Presence) -> "PresenceOut":
        return cls(
            present=p.present,
            place_name=p.place_name,
            last_seen=p.last_seen.isoformat() if p.last_seen is not None else None,
            age_seconds=p.age_seconds,
            stale=p.stale,
        )


@router.get("/presence")
async def presence(request: Request, principal: PrincipalDep) -> PresenceOut:
    """The owner's OWN latest place + freshness, for the app-open presence toast.
    Owner-only and full-owner gated (`read_owner_presence` calls `require_full_owner`
    before any read). Freshness-honest: a stale fix is flagged so the toast reads
    "last known", never "here now". Coordinate-free."""
    p = await read_owner_presence(
        get_location_repo(request), get_sql_device_repo(request), ctx_for(principal)
    )
    return PresenceOut.of(p)
