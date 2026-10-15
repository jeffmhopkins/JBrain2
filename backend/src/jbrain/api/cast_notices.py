"""Notices on the house's Chromecast TVs: the TVs, send, Sent, the frame, Take it down.

docs/plans/JPANEL_CAST_PLAN.md (K1), mock docs/mocks/jpanel-cast/. Two routers over one service
(`jbrain.cast.service`):

- `router` — the owner (`OwnerDep`): `GET /cast/tvs`, `POST /cast/send`, `GET /cast/sent`,
  `GET /cast/{id}`, `GET /cast/{id}/frame`, `POST /cast/{id}/stop`, and `POST /cast/preview`
  (the frame the TVs would get, nothing cast).
- `debug_router` — a debug token (`DebugDep`, mounted only with debug access on):
  `POST /debug/cast/send` and `GET /debug/cast/sent`, so a session holding the token can cast
  a real notice and read how each TV took it without a terminal (CLAUDE.md #10).

Refusals are `{"detail": {"reason": …}}`: 422 for a field, 415 for a picture that is not one,
503 when the sidecar is not there.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep, OwnerDep
from jbrain.cast import frame as F
from jbrain.cast.service import KEEP_S, CastRefused, CastService, CastView
from jbrain.db.session import SessionContext

router = APIRouter(prefix="/cast", tags=["cast"])
debug_router = APIRouter(prefix="/debug/cast")

#: A phone photo, base64; the frame is drawn from it, so a bigger one buys nothing.
PICTURE_MAX = 8 * 1024 * 1024
#: How the top line of the frame names a sender (the mock: "Dad · via jbrain").
SENDER_LABELS = {"owner": "Dad", "jerv": "Jerv", "ops": "Ops", "debug": "jbrain test"}


def service(request: Request) -> CastService:
    return cast(CastService, request.app.state.cast_notices)


class CastIn(BaseModel):
    tv_ids: list[str] = Field(min_length=1, max_length=16)
    title: str = Field(max_length=480)
    body: str = Field(default="", max_length=2400)
    #: A PNG, JPEG or WebP, base64.
    picture_b64: str | None = None
    #: How long it stays up before the box takes it down (5-300 s).
    seconds: int = Field(default=30, ge=5, le=300)
    #: The whole 16:9 frame, drawn on the phone (the composer's photo with its words placed
    #: over it), base64. With it, `body` and `picture_b64` are not drawn; `title` names it.
    frame_b64: str | None = None


class PreviewIn(BaseModel):
    title: str = Field(max_length=480)
    body: str = Field(default="", max_length=2400)
    picture_b64: str | None = None


class TvOut(BaseModel):
    tv_id: str
    name: str
    outcome: str
    why: str


class CastOut(BaseModel):
    id: str
    sender: str
    title: str
    body: str
    seconds: int
    created_at: datetime
    stopped_at: datetime | None
    tvs: list[TvOut]


class CastList(BaseModel):
    notices: list[CastOut]
    kept_s: int


def _out(v: CastView) -> CastOut:
    return CastOut(
        id=v.id,
        sender=v.sender,
        title=v.title,
        body=v.body,
        seconds=v.seconds,
        created_at=v.created_at,
        stopped_at=v.stopped_at,
        tvs=[TvOut(tv_id=t.tv_id, name=t.name, outcome=t.outcome, why=t.why) for t in v.tvs],
    )


def _picture(value: str | None, field: str = "picture_b64") -> bytes | None:
    if value is None:
        return None
    if len(value) > PICTURE_MAX * 4 // 3 + 4:
        raise HTTPException(413, detail={"reason": "picture_too_big", "max_bytes": PICTURE_MAX})
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(
            422, detail={"reason": "bad_field", "field": field, "want": "base64"}
        ) from None


async def _meta(request: Request, sender: str) -> str:
    """The frame's top line: who it is from and the house's time."""
    zone = await request.app.state.settings_store.owner_timezone(
        SessionContext(principal_kind="owner")
    )
    now = datetime.now(ZoneInfo(zone) if zone else UTC)
    who = SENDER_LABELS.get(sender, sender.removeprefix("workflow:"))
    return f"{who} · via jbrain · {now:%H:%M}"


async def send(request: Request, body: CastIn, sender: str) -> CastOut:
    try:
        view = await service(request).send(
            sender=sender,
            title=body.title,
            body=body.body,
            picture=_picture(body.picture_b64),
            tv_ids=body.tv_ids,
            seconds=body.seconds,
            meta=await _meta(request, sender),
            frame=_picture(body.frame_b64, "frame_b64"),
        )
    except CastRefused as exc:
        raise HTTPException(exc.status, detail=exc.detail()) from None
    return _out(view)


async def sent(request: Request, limit: int) -> CastList:
    svc = service(request)
    await svc.sweep()
    return CastList(notices=[_out(v) for v in await svc.history(limit)], kept_s=KEEP_S)


# ---- the owner -----------------------------------------------------------------------------


@router.get("/tvs")
async def owner_tvs(request: Request, _o: OwnerDep) -> dict[str, Any]:
    """Every TV the box has found, with its verdict (idle / standby / busy / unknown) and why,
    live from the sidecar."""
    return await service(request).tvs()


@router.post("/send")
async def owner_send(body: CastIn, request: Request, _o: OwnerDep) -> CastOut:
    """Cast a notice to the chosen TVs now. Each TV's gate is checked at the instant of
    casting; the reply says how each took it (`shown`, `woke`, `skipped` with why, `failed`)."""
    return await send(request, body, "owner")


@router.get("/sent")
async def owner_sent(
    request: Request, _o: OwnerDep, limit: Annotated[int, Query(ge=1, le=100)] = 30
) -> CastList:
    """What was cast, newest first, each with how every TV took it. Kept a week."""
    return await sent(request, limit)


@router.post("/preview", response_model=None)
async def owner_preview(body: PreviewIn, request: Request, _o: OwnerDep) -> Response:
    """The frame the TVs would be cast, as a PNG; nothing is sent."""
    picture = _picture(body.picture_b64)
    meta = await _meta(request, "owner")
    try:
        png = await asyncio.to_thread(F.render, body.title, body.body, picture, meta=meta)
    except F.FrameError:
        raise HTTPException(415, detail={"reason": "bad_picture"}) from None
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/{notice_id}")
async def owner_get(notice_id: str, request: Request, _o: OwnerDep) -> CastOut:
    view = await service(request).get(_uuid(notice_id))
    if view is None:
        raise HTTPException(404, detail={"reason": "no_such_notice"})
    return _out(view)


@router.get("/{notice_id}/frame", response_model=None)
async def owner_frame(notice_id: str, request: Request, _o: OwnerDep) -> Response:
    """The frame the TVs were cast. It never changes, so the phone may keep it."""
    png = await service(request).frame(_uuid(notice_id))
    if png is None:
        raise HTTPException(404, detail={"reason": "no_such_notice"})
    return Response(
        content=png,
        media_type=F.mime_of(png),
        headers={"Cache-Control": "private, max-age=604800, immutable"},
    )


@router.post("/{notice_id}/stop")
async def owner_stop(notice_id: str, request: Request, _o: OwnerDep) -> dict[str, Any]:
    """Take it down: quit the notice on every TV still showing it, and only there."""
    stopped = await service(request).stop(_uuid(notice_id))
    if stopped is None:
        raise HTTPException(404, detail={"reason": "no_such_notice"})
    return {"id": notice_id, "stopped": stopped}


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(404, detail={"reason": "no_such_notice"}) from None


# ---- a debug token ---------------------------------------------------------------------------


@debug_router.post("/send")
async def debug_send(body: CastIn, request: Request, _p: DebugDep) -> CastOut:
    """Cast a real notice as `debug` ("jbrain test" on the frame), gated like the owner's."""
    request.state.debug_detail = f"cast send {len(body.tv_ids)} tv(s)"
    return await send(request, body, "debug")


@debug_router.get("/sent")
async def debug_sent(
    request: Request, _p: DebugDep, limit: Annotated[int, Query(ge=1, le=100)] = 30
) -> CastList:
    request.state.debug_detail = "cast sent"
    return await sent(request, limit)
