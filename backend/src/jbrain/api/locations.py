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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jbrain.api.deps import PrincipalDep, owner_only
from jbrain.api.notes import ctx_for
from jbrain.devices.repo import DeviceInfo, DeviceRepo
from jbrain.locations import (
    DeviceActivity,
    FixPoint,
    PlaceGeofence,
    SqlLocationRepo,
    TimelineEntry,
)

router = APIRouter(prefix="/locations", dependencies=[Depends(owner_only)])

_DEFAULT_TIMELINE_WINDOW = timedelta(days=30)
_DEFAULT_FIXES_WINDOW = timedelta(days=1)
# Bound a single read so an over-wide window can never stream the whole hypertable
# (the owner can page by narrowing the date range).
_FIXES_LIMIT = 20_000
_TIMELINE_LIMIT = 500


def get_location_repo(request: Request) -> SqlLocationRepo:
    return cast(SqlLocationRepo, request.app.state.location_repo)


def get_device_repo(request: Request) -> DeviceRepo:
    return cast(DeviceRepo, request.app.state.device_repo)


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
    rows = await get_location_repo(request).fixes(
        ctx_for(principal), subject_id=subject_id, since=start, until=end, limit=_FIXES_LIMIT
    )
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
