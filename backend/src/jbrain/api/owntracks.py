"""OwnTracks HTTP ingestion (Phase 7 Wave 3a; batch in the location-detail plan).

A provisioned device POSTs `_type:location` reports here over HTTP Basic (the
device key as password). The body is EITHER a single OwnTracks object (stock
OwnTracks) OR a JSON array of them (our app's batched upload, so a dense fix stream
costs one request). Authentication is the `DeviceDep` dependency (401 pre-auth);
past auth the endpoint always answers 200 with the OwnTracks-expected array so the
client never enters a retry storm over a transient downstream error, EXCEPT a 429
when a device floods (OwnTracks then backs off), a 422 for malformed JSON / a
schema-invalid location body / an over-large batch, and a 413 for an over-size body.
A batch's elements are all schema-validated before any write (one bad element
rejects the batch — never a partial-trust store); each fix then inserts in its own
idempotent (`ON CONFLICT DO NOTHING`) transaction, so a whole-batch retry after a
mid-batch error safely dedups rather than double-storing. The device subject is
code-set from the authenticated principal for every element, never the payload (L9).
The parse/store/geofence logic is the shared ingest core (`jbrain.locations.ingest`)
the MQTT consumer also feeds.
"""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import DeviceDep
from jbrain.locations import SqlLocationRepo
from jbrain.locations.ingest import OwnTracksLocation, ingest_location, is_location_message
from jbrain.locations.live import LiveBroadcaster, live_fix_from_owntracks
from jbrain.locations.ratelimit import TokenBucket

router = APIRouter()

# A single POST may carry at most this many reports — bounds the work per request
# and keeps a batch's token cost under the per-device bucket capacity. The app
# chunks a long offline backlog into batches no larger than this.
MAX_BATCH = 100

# A hard ceiling on the request body, checked from Content-Length before parsing so
# a giant/deeply-nested array can't force unbounded JSON work. Generous headroom
# over a full MAX_BATCH batch (~300 B/fix ⇒ ~30 KB).
MAX_BODY_BYTES = 256 * 1024


def get_location_repo(request: Request) -> SqlLocationRepo:
    return cast(SqlLocationRepo, request.app.state.location_repo)


def get_rate_limiter(request: Request) -> TokenBucket:
    return cast(TokenBucket, request.app.state.location_rate_limiter)


def get_session_maker(request: Request) -> "async_sessionmaker[AsyncSession]":
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


def get_live_broadcaster(request: Request) -> LiveBroadcaster | None:
    """The in-process live fan-out, when wired (always in the real app; absent in some
    unit tests). HTTP-ingested fixes publish here so the dashboard's live socket sees
    them in real time — the MQTT path has its own feeder, so an HTTP fix is never the
    feeder's fix and there's no double publish."""
    return cast(LiveBroadcaster | None, getattr(request.app.state, "live_broadcaster", None))


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
    # Bound the work BEFORE parsing the body: a cheap one-token gate stops a
    # post-auth flood, and a Content-Length cap bounds a single request's JSON parse
    # (an authenticated device otherwise forces unbounded parsing of a giant array).
    if not limiter.allow(principal.id):
        raise HTTPException(status_code=429, detail="rate limited")
    clen = request.headers.get("content-length")
    if clen is not None and clen.isdigit() and int(clen) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        body = await request.json()
    except ValueError as exc:  # malformed JSON: 422, never a 500
        raise HTTPException(status_code=422, detail="invalid json") from exc
    # Single object (stock OwnTracks) or an array of them (our batched upload).
    items = body if isinstance(body, list) else [body]
    if len(items) > MAX_BATCH:
        raise HTTPException(status_code=422, detail="batch too large")
    # Only `_type:location` items are stored; transition / waypoints / lwt / anything
    # else is acked and ignored (server-side geofencing is authoritative).
    locations = [item for item in items if is_location_message(item)]
    # One token per fix: the gate above charged one; charge any remainder so a batch
    # costs its fix count. A flooding device 429s and backs off.
    if len(locations) > 1 and not limiter.allow(principal.id, cost=len(locations) - 1):
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
    # A newly-stored fix also fans out to the live socket so the dashboard map moves in
    # real time (the live feeder only sees MQTT, so the HTTP path must publish itself).
    broadcaster = get_live_broadcaster(request)
    for item in locations:
        inserted = await ingest_location(
            repo,
            maker,
            principal_id=principal.id,
            subject_id=principal.subject_id,
            body=item,
            notifier=getattr(request.app.state, "push_notifier", None),
        )
        if inserted and broadcaster is not None:
            fix = live_fix_from_owntracks(principal.subject_id, item)
            if fix is not None:
                broadcaster.publish(fix)
    return []
