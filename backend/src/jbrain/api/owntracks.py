"""OwnTracks HTTP ingestion (Phase 7 Wave 3a).

A provisioned device POSTs `_type:location` reports here over HTTP Basic (the
device key as password). Authentication is the `DeviceDep` dependency (401
pre-auth); past auth the endpoint always answers 200 with the OwnTracks-expected
array so the client never enters a retry storm over a transient downstream error,
EXCEPT a 429 when a device floods (OwnTracks then backs off) and a 422 for a
schema-invalid location body. The parse/store/geofence logic is the shared ingest
core (`jbrain.locations.ingest`) the MQTT consumer also feeds.
"""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import DeviceDep
from jbrain.locations import SqlLocationRepo
from jbrain.locations.ingest import ingest_location, is_location_message
from jbrain.locations.ratelimit import TokenBucket

router = APIRouter()


def get_location_repo(request: Request) -> SqlLocationRepo:
    return cast(SqlLocationRepo, request.app.state.location_repo)


def get_rate_limiter(request: Request) -> TokenBucket:
    return cast(TokenBucket, request.app.state.location_rate_limiter)


def get_session_maker(request: Request) -> "async_sessionmaker[AsyncSession]":
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


LocationRepoDep = Annotated[SqlLocationRepo, Depends(get_location_repo)]
RateLimiterDep = Annotated[TokenBucket, Depends(get_rate_limiter)]
SessionMakerDep = Annotated["async_sessionmaker[AsyncSession]", Depends(get_session_maker)]


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
    if not is_location_message(body):
        return []  # transition / waypoints / anything else: ack and ignore
    try:
        # The device subject is code-set from the authenticated principal, never
        # the payload (L9); a dup (idempotent retry) is a no-op. A crossing fires a
        # content-free poke when FCM is configured (app.state.push_notifier, M6).
        await ingest_location(
            repo,
            maker,
            principal_id=principal.id,
            subject_id=principal.subject_id,
            body=body,
            notifier=getattr(request.app.state, "push_notifier", None),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid location") from exc
    return []
