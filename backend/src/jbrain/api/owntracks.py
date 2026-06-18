"""OwnTracks HTTP ingestion (Phase 7 Wave 3a).

A provisioned device POSTs `_type:location` reports here over HTTP Basic (the
device key as password). Authentication is the `DeviceDep` dependency (401
pre-auth); past auth the endpoint always answers 200 with the OwnTracks-expected
array so the client never enters a retry storm over a transient downstream error,
EXCEPT a 429 when a device floods (OwnTracks then backs off) and a 422 for a
schema-invalid location body. Non-`location` messages (transition, waypoints) are
acknowledged and ignored — server-side geofencing (Wave 3b) is authoritative.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import DeviceDep
from jbrain.locations import LocationFix, SqlLocationRepo
from jbrain.locations.geofence import detect_transitions
from jbrain.locations.ratelimit import TokenBucket

router = APIRouter()

# A reported capture instant this far ahead of the server clock is bogus (a
# spoof or a wildly wrong device clock); drop it. Past fixes are kept verbatim
# (historical/offline backfill is legitimate).
MAX_FUTURE_SKEW = timedelta(hours=24)


def get_location_repo(request: Request) -> SqlLocationRepo:
    return cast(SqlLocationRepo, request.app.state.location_repo)


def get_rate_limiter(request: Request) -> TokenBucket:
    return cast(TokenBucket, request.app.state.location_rate_limiter)


def get_session_maker(request: Request) -> "async_sessionmaker[AsyncSession]":
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


LocationRepoDep = Annotated[SqlLocationRepo, Depends(get_location_repo)]
RateLimiterDep = Annotated[TokenBucket, Depends(get_rate_limiter)]
SessionMakerDep = Annotated["async_sessionmaker[AsyncSession]", Depends(get_session_maker)]


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


def _to_fix(p: OwnTracksLocation) -> LocationFix:
    return LocationFix(
        captured_at=datetime.fromtimestamp(p.tst, UTC),
        latitude=p.lat,
        longitude=p.lon,
        accuracy_m=p.acc,
        altitude_m=p.alt,
        velocity_mps=p.vel / 3.6 if p.vel is not None else None,  # km/h -> m/s
        course_deg=p.cog,
        battery_pct=p.batt,
        connection=p.conn,
        tracker_id=p.tid,
        raw=p.model_dump(exclude_none=True),  # allowlisted to the declared fields
    )


@router.post("/owntracks")
async def owntracks(
    request: Request,
    principal: DeviceDep,
    repo: LocationRepoDep,
    limiter: RateLimiterDep,
    maker: SessionMakerDep,
) -> list[Any]:
    if not limiter.allow(principal.id):
        raise HTTPException(status_code=429, detail="rate limited")
    body = await request.json()
    if not isinstance(body, dict) or body.get("_type") != "location":
        return []  # transition / waypoints / anything else: ack and ignore
    try:
        payload = OwnTracksLocation.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid location") from exc

    fix = _to_fix(payload)
    if fix.captured_at <= datetime.now(UTC) + MAX_FUTURE_SKEW:
        # The device subject is code-set from the authenticated principal, never
        # from the payload (L9). A dup (idempotent retry) is a no-op.
        inserted = await repo.ingest_fix(
            principal_id=principal.id, subject_id=principal.subject_id, fix=fix
        )
        # Detect geofence crossings only on a genuinely new fix (a retry must not
        # re-fire transitions). detect_transitions is best-effort internally.
        if inserted:
            await detect_transitions(
                maker,
                principal_id=principal.id,
                subject_id=principal.subject_id,
                captured_at=fix.captured_at,
                latitude=fix.latitude,
                longitude=fix.longitude,
                accuracy_m=fix.accuracy_m,
            )
    return []
