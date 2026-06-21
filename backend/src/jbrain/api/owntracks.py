"""OwnTracks HTTP ingestion (Phase 7 Wave 3a; batch in the location-detail plan).

A provisioned device POSTs `_type:location` reports here over HTTP Basic (the
device key as password). The body is EITHER a single OwnTracks object (stock
OwnTracks) OR a JSON array of them (our app's batched upload, so a dense fix stream
costs one request). Authentication is the `DeviceDep` dependency (401 pre-auth);
past auth the endpoint always answers 200 with the OwnTracks-expected array so the
client never enters a retry storm over a transient downstream error, EXCEPT a 429
when a device floods (OwnTracks then backs off) and a 422 for a schema-invalid
location body or an over-large batch. A batch is validated WHOLE before any write
(one bad element rejects the batch — never a partial-trust write); the device
subject is code-set from the authenticated principal for every element, never the
payload (L9). The parse/store/geofence logic is the shared ingest core
(`jbrain.locations.ingest`) the MQTT consumer also feeds.
"""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import DeviceDep
from jbrain.locations import SqlLocationRepo
from jbrain.locations.ingest import OwnTracksLocation, ingest_location, is_location_message
from jbrain.locations.ratelimit import TokenBucket

router = APIRouter()

# A single POST may carry at most this many reports — bounds the work per request
# and keeps a batch's token cost under the per-device bucket capacity. The app
# chunks a long offline backlog into batches no larger than this.
MAX_BATCH = 100


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
    body = await request.json()
    # Single object (stock OwnTracks) or an array of them (our batched upload).
    items = body if isinstance(body, list) else [body]
    if len(items) > MAX_BATCH:
        raise HTTPException(status_code=422, detail="batch too large")
    # Only `_type:location` items are stored; transition / waypoints / lwt / anything
    # else is acked and ignored (server-side geofencing is authoritative).
    locations = [item for item in items if is_location_message(item)]
    # One token per fix (an empty/non-location post still costs one) — a flooding
    # device 429s and backs off; a normal batch every few seconds never trips it.
    if not limiter.allow(principal.id, cost=max(1, len(locations))):
        raise HTTPException(status_code=429, detail="rate limited")
    # Validate the WHOLE batch before any write: one schema-invalid element rejects
    # the batch (422), so a bad element can never leave a partial-trust write behind.
    try:
        for item in locations:
            OwnTracksLocation.model_validate(item)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid location") from exc
    # Store oldest-first. The device subject is code-set from the authenticated
    # principal, never the payload (L9); a dup (idempotent retry) is a no-op. A
    # crossing fires a content-free poke when FCM is configured (push_notifier, M6).
    for item in locations:
        await ingest_location(
            repo,
            maker,
            principal_id=principal.id,
            subject_id=principal.subject_id,
            body=item,
            notifier=getattr(request.app.state, "push_notifier", None),
        )
    return []
