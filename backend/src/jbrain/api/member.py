"""The member dashboard API (JBrain360 M4b): a family member's scoped reads.

Device-cookie gated (`/session/mint` mints it). Every read runs under the
member's `device_context` — a non-owner, subject-pinned session — so Postgres RLS
is the firewall: a member sees its own track plus its family group's
(`viewer_may_see`) and nothing else, even if it asks for an arbitrary subject.

This surface is **positions + presence only** (owner decision: per-place sharing
gates the timeline + fence overlay, which arrive in M4c). The member history is
capped to a **30-day trailing window** server-side (plan B5) — clamped here, never
trusted to the client — while the owner's `/locations` reads stay uncapped.
"""

from datetime import UTC, datetime, timedelta
from typing import cast

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from jbrain.api.deps import MemberDep
from jbrain.api.locations import PlaceOut, TimelineEntryOut
from jbrain.db.session import device_context
from jbrain.locations import FixPoint, MemberSubject, SqlLocationRepo
from jbrain.push import SqlFcmTokenRepo

router = APIRouter(prefix="/member")
log = structlog.get_logger()

# The member dashboard sees at most a 30-day trailing window (plan B5). The owner
# is uncapped; this clamp is the member-only retention boundary, enforced server
# side so a crafted `since` can never reach older history.
_MEMBER_HISTORY_CAP = timedelta(days=30)
_FIXES_LIMIT = 20_000
_TIMELINE_LIMIT = 500


def _repo(request: Request) -> SqlLocationRepo:
    return cast(SqlLocationRepo, request.app.state.location_repo)


def _fcm_repo(request: Request) -> SqlFcmTokenRepo:
    return cast(SqlFcmTokenRepo, request.app.state.fcm_token_repo)


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid timestamp: {ts!r}") from exc


class MemberSubjectOut(BaseModel):
    subject_id: str
    label: str
    last_seen: str | None
    battery_pct: int | None
    connection: str | None

    @classmethod
    def of(cls, m: MemberSubject) -> "MemberSubjectOut":
        return cls(
            subject_id=m.subject_id,
            label=m.label,
            last_seen=m.last_seen.isoformat() if m.last_seen else None,
            battery_pct=m.battery_pct,
            connection=m.connection,
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


@router.get("/roster")
async def roster(request: Request, principal: MemberDep) -> list[MemberSubjectOut]:
    """The subjects this member may see (itself + its family group) with each one's
    label and latest activity — the map's device-picker + presence roster."""
    ctx = device_context(principal.id, principal.subject_id)
    rows = await _repo(request).member_roster(ctx, viewer_subject_id=principal.subject_id)
    return [MemberSubjectOut.of(m) for m in rows]


@router.get("/positions")
async def positions(
    request: Request,
    principal: MemberDep,
    subject_id: str,
    since: str | None = None,
    until: str | None = None,
) -> list[FixPointOut]:
    """A visible subject's fixes in `[since, until)`, oldest first — the map trail.
    Clamped to the 30-day trailing cap; RLS returns nothing for a subject this
    member may not see (so an arbitrary `subject_id` yields an empty trail)."""
    end = _parse(until) or datetime.now(UTC)
    floor = datetime.now(UTC) - _MEMBER_HISTORY_CAP
    start = _parse(since) or floor
    if start < floor:  # the member retention boundary, never trusted to the client
        start = floor
    ctx = device_context(principal.id, principal.subject_id)
    repo = _repo(request)
    rows = await repo.fixes(ctx, subject_id=subject_id, since=start, until=end, limit=_FIXES_LIMIT)
    # Who-saw-whom: a member reading a track is an audited view (M3a). Best-effort —
    # an audit-write failure is logged, never a 500 on the read.
    try:
        await repo.record_view(
            ctx,
            viewer_principal_id=principal.id,
            viewer_subject_id=principal.subject_id,
            target_subject_id=subject_id,
            path="history",
        )
    except Exception as exc:  # noqa: BLE001 - audit failure is logged, never a 500
        log.warning("member.audit_failed", error=repr(exc))
    return [FixPointOut.of(f) for f in rows]


@router.get("/places")
async def places(request: Request, principal: MemberDep) -> list[PlaceOut]:
    """The owner-shared geofences only, for the member map's fence overlay. An
    un-shared (or owner-private) fence is never named to a member (M4c)."""
    ctx = device_context(principal.id, principal.subject_id)
    rows = await _repo(request).member_places(ctx)
    return [PlaceOut.of(p) for p in rows]


@router.get("/timeline")
async def timeline(
    request: Request,
    principal: MemberDep,
    since: str | None = None,
    until: str | None = None,
) -> list[TimelineEntryOut]:
    """The member's 'arrived/left <place>' feed, newest first — crossings at SHARED
    places for the subjects this member may see. Clamped to the 30-day cap."""
    end = _parse(until) or datetime.now(UTC)
    floor = datetime.now(UTC) - _MEMBER_HISTORY_CAP
    start = _parse(since) or floor
    if start < floor:
        start = floor
    ctx = device_context(principal.id, principal.subject_id)
    rows = await _repo(request).member_timeline(
        ctx, viewer_subject_id=principal.subject_id, since=start, until=end, limit=_TIMELINE_LIMIT
    )
    return [TimelineEntryOut.of(e) for e in rows]


class FcmTokenIn(BaseModel):
    token: str


@router.put("/fcm-token", status_code=204)
async def register_fcm_token(request: Request, principal: MemberDep, body: FcmTokenIn) -> None:
    """Register/refresh this device's FCM token for content-free pokes (M6). RLS pins
    the row to the device's own subject — a device can't register under another."""
    ctx = device_context(principal.id, principal.subject_id)
    await _fcm_repo(request).register(
        ctx, principal_id=principal.id, subject_id=principal.subject_id, token=body.token
    )


@router.delete("/fcm-token", status_code=204)
async def delete_fcm_token(request: Request, principal: MemberDep, body: FcmTokenIn) -> None:
    """Drop this device's token (sign-out / push opt-out)."""
    ctx = device_context(principal.id, principal.subject_id)
    await _fcm_repo(request).delete(ctx, token=body.token)
