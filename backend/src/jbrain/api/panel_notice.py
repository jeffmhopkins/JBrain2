"""Notices on the owner's desk panel: send one, read the answer, and the panel's side.

JPANEL_C6_PLAN.md §2 (wave C3b). The owner: *"an API … that just shows a pop-up of a message …
maybe even with a response of choices"*, a picture the sender drew, a sound of up to eight
seconds, hidden by a tap or by a timeout. Three routers, one service (`jpanel.notice_service`):

- `router` — the owner (`OwnerDep`): `POST /panel/notify`, `GET /panel/notify/{id}?wait=`, and
  the composer's (2026-10-14): `GET /panel/notify?limit=&device_id=` (his own notices, newest
  first), `GET /panel/notify/{id}/image` (the picture as the panel draws it), `POST
  /panel/notify/{id}/withdraw` (Unsend) and `POST /panel/notify/preview` (the box's encoder
  over a picture, nothing sent). No panel key reaches any of them: `OwnerDep` refuses one.
- `debug_router` — a debug token (`DebugDep`, mounted only with debug access on):
  `POST /debug/panel/{device_id}/notice`, `GET /debug/panel/{device_id}/notice/{id}?wait=`,
  so a session holding the token can put a test notice on the glass and read the tap.
- `panel_router` — the panel's own key (`PanelDep`): `GET /endpoint/notice/next`,
  `GET /endpoint/notice/{id}/body`, `POST /endpoint/notice/{id}/state`. Every read and write
  runs under the PANEL's context, so the table's policies bound these routes (CLAUDE.md #3).

Refusals are `{"detail": {"reason": …}}` with the status the plan's table gives them: 503
`offline` (+ `last_seen_s`), 409 `not_accepting`, 409 `quiet_hours` (+ `until`, `until_at`), 409
`no_notice_surface`, 429 `busy`, 413/415/422 for a picture, sound or field the panel cannot take.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from typing import Annotated, Any, Literal, cast

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep, OwnerDep, PanelDep
from jbrain.api.notes import ctx_for
from jbrain.auth.service import PrincipalInfo
from jbrain.jpanel import debug_relay as relay
from jbrain.jpanel import notice as N
from jbrain.jpanel.notice_service import NoticeService, NoticeView, Sent

log = structlog.get_logger()

router = APIRouter(prefix="/panel/notify", tags=["panel"])
debug_router = APIRouter(prefix="/debug/panel")
panel_router = APIRouter(prefix="/endpoint/notice", tags=["endpoint"])

MEDIA_TYPE = "application/x-jpanel-notice"


def service(request: Request) -> NoticeService:
    return cast(NoticeService, request.app.state.panel_notices)


def _refuse(exc: N.NoticeRefused) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail())


def _b64(field: str, value: str | None, cap: int) -> bytes | None:
    if value is None:
        return None
    # Base64 is 4/3 of the bytes; refuse an oversized one before decoding it.
    if len(value) > cap * 4 // 3 + 4:
        raise N.NoticeRefused(f"{field}_too_big", 413, max_bytes=cap)
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise N.NoticeRefused("bad_field", 422, field=f"{field}_b64", want="base64") from None


class NoticeIn(BaseModel):
    """One notice. Text limits are in UTF-8 bytes and checked by the service, so the reason
    names the field; the lengths here only stop an absurd body early."""

    #: Which panel; omitted, the one panel that takes notices (refused if there are several).
    device_id: str | None = None
    #: Who it is from, as the panel names it: jerv, ops, owner, debug, workflow:<name>.
    sender: str = "owner"
    title: str = Field(max_length=240)
    body: str = Field(default="", max_length=1200)
    #: 0-4 answers, ≤ 16 bytes each; the tapped one goes back to the sender.
    choices: list[str] = Field(default_factory=list, max_length=8)
    #: Chime as it arrives (still silent inside quiet hours — a refusal is never bypassed).
    chime: bool = False
    #: Hide this long after it is shown (3-600 s); 0: until it is tapped; omitted: 20 s for a
    #: plain notice, until answered for a question (owner, 2026-10-13).
    hide_after_s: int | None = None
    #: How long an answer is accepted once the panel has it (30 s - 24 h, default 15 min).
    ttl_s: int | None = None
    #: A PNG, JPEG or WebP, base64.
    image_b64: str | None = None
    #: full: the whole glass; banner: 480 wide over the words; auto: full when there is no
    #: body and no choices, else banner.
    image_mode: Literal["auto", "full", "banner"] = "auto"
    #: A sound of ≤ 8 s, base64: a PCM WAV, or MP3/Ogg/Opus/M4A/FLAC through ffmpeg.
    audio_b64: str | None = None
    #: Or words to speak in the PWA's read-aloud voice (≤ 200 characters, ≤ 8 s spoken).
    say: str | None = None


class NoticeSent(BaseModel):
    id: str
    device_id: str
    panel: str
    state: str
    expires_in_s: int
    image: dict[str, Any] | None
    audio_ms: int | None


class NoticeOut(BaseModel):
    id: str
    device_id: str
    sender: str
    title: str
    body: str
    choices: list[str]
    state: str
    answer: str | None
    created_at: str
    delivered_at: str | None
    shown_at: str | None
    finished_at: str | None
    hide_after_s: int | None
    image: dict[str, Any] | None
    audio_ms: int | None
    expires_at: str | None
    ttl_s: int
    chime: bool
    kind: str


class NoticeList(BaseModel):
    notices: list[NoticeOut]
    #: How long the box keeps a finished one of these, so the list can say so.
    kept_s: int


class PreviewIn(BaseModel):
    image_b64: str
    image_mode: Literal["full", "banner"] = "full"


class PreviewOut(BaseModel):
    #: The picture exactly as the panel will draw it (fitted, rgb565, posterized if it had to
    #: be), as a base64 PNG.
    png_b64: str
    w: int
    h: int
    bytes: int
    posterize: int


def _sent(s: Sent) -> NoticeSent:
    return NoticeSent(**s.__dict__)


def _out(v: NoticeView) -> NoticeOut:
    return NoticeOut(**v.as_dict())


async def send(request: Request, body: NoticeIn, *, sender: str, created_by: str) -> NoticeSent:
    svc = service(request)
    try:
        image = _b64("image", body.image_b64, N.IMAGE_UPLOAD_MAX)
        audio = _b64("audio", body.audio_b64, N.AUDIO_UPLOAD_MAX)
        sent = await svc.send(
            sender=sender,
            created_by=created_by,
            title=body.title,
            body=body.body,
            choices=body.choices,
            chime=body.chime,
            hide_after_s=body.hide_after_s,
            ttl_s=body.ttl_s,
            image=image,
            image_mode=body.image_mode,
            audio=audio,
            say=body.say,
            device_id=body.device_id,
        )
    except N.NoticeRefused as exc:
        raise _refuse(exc) from None
    return _sent(sent)


async def read(
    request: Request, notice_id: str, wait: float, device_id: str | None = None
) -> NoticeOut:
    svc = service(request)
    await svc.sweep()
    view = await svc.wait(notice_id, wait, device_id)
    if view is None:
        raise HTTPException(status_code=404, detail={"reason": "no_such_notice"})
    return _out(view)


# ---- the owner -----------------------------------------------------------------------------


@router.post("", status_code=202)
async def owner_send(body: NoticeIn, request: Request, owner: OwnerDep) -> NoticeSent:
    """Put a notice on the panel. 202 `{id, state: "queued", …}`; poll `GET …/{id}?wait=`.
    The owner may send as any sender — the PWA's test control sends as `owner`."""
    return await send(request, body, sender=body.sender, created_by=f"owner ({owner.label})")


@router.get("")
async def owner_history(
    request: Request,
    _o: OwnerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    device_id: str | None = None,
) -> NoticeList:
    """The owner's own notices (sender `owner`, the composer's), newest first, with how each
    ended — the composer's "Sent to this panel" (`device_id` narrows it to one panel). Kept a
    week after they end."""
    svc = service(request)
    await svc.sweep()
    views = await svc.history(limit, device_id)
    return NoticeList(notices=[_out(v) for v in views], kept_s=N.OWNER_RESULT_TTL_S)


@router.post("/preview")
async def owner_preview(body: PreviewIn, request: Request, _o: OwnerDep) -> PreviewOut:
    """Run the box's own fitting and encoding over a picture without sending anything, so
    Review can show what the panel will draw — a photograph posterized to fit the panel's cap
    looks different from the phone's copy."""
    try:
        data = _b64("image", body.image_b64, N.IMAGE_UPLOAD_MAX) or b""
        fitted, png = await asyncio.to_thread(N.preview_png, data, body.image_mode)
    except N.NoticeRefused as exc:
        raise _refuse(exc) from None
    return PreviewOut(
        png_b64=base64.b64encode(png).decode(),
        w=fitted.width,
        h=fitted.height,
        bytes=fitted.stream_bytes,
        posterize=fitted.posterize,
    )


@router.get("/{notice_id}/image", response_model=None)
async def owner_image(
    notice_id: str,
    request: Request,
    _o: OwnerDep,
    w: Annotated[int, Query(ge=32, le=N.SCREEN)] = 160,
) -> Response:
    """The notice's picture as the panel draws it, decoded from the stored stripes, as a PNG at
    most `w` wide. A notice's picture never changes, so the phone may keep it."""
    png = await service(request).image_png(notice_id, w)
    if png is None:
        raise HTTPException(status_code=404, detail={"reason": "no_picture"})
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=604800, immutable"},
    )


@router.post("/{notice_id}/withdraw")
async def owner_withdraw(notice_id: str, request: Request, owner: OwnerDep) -> NoticeOut:
    """Unsend: take a queued, delivered or shown notice back off the panel. A queued one is
    never delivered; one the panel holds goes on its next pickup, silently. 404 for no such
    notice; 409 `already_finished` (+ `state`) for one that has already ended."""
    try:
        view = await service(request).withdraw(notice_id)
    except N.NoticeRefused as exc:
        raise _refuse(exc) from None
    log.info("panel.notice_unsent", id=notice_id, by=owner.label)
    return _out(view)


@router.get("/{notice_id}")
async def owner_read(
    notice_id: str,
    request: Request,
    _o: OwnerDep,
    wait: Annotated[float, Query(ge=0, le=N.MAX_WAIT_S)] = 0,
) -> NoticeOut:
    """The notice's state and answer. `wait` (≤ 25 s) holds the call until it is answered,
    dismissed or expired; a call that returns an open state is simply asked again."""
    return await read(request, notice_id, wait)


# ---- the debug console -------------------------------------------------------------------------


@debug_router.post("/{device_id}/notice", status_code=202)
async def debug_send(
    device_id: str, body: NoticeIn, request: Request, principal: DebugDep
) -> NoticeSent:
    """A test notice from a debug session. Sent as `debug` unless the body names another
    sender; the token's label is recorded on the row and the log line."""
    request.state.debug_detail = f"panel {device_id[:8]} notice"
    body = body.model_copy(update={"device_id": device_id})
    sender = body.sender if body.sender != "owner" else "debug"
    out = await send(request, body, sender=sender, created_by=f"{principal.label} ({principal.id})")
    log.info("debug.panel_notice", token=principal.label, device=device_id, id=out.id)
    return out


@debug_router.get("/{device_id}/notice/{notice_id}")
async def debug_read(
    device_id: str,
    notice_id: str,
    request: Request,
    _p: DebugDep,
    wait: Annotated[float, Query(ge=0, le=N.MAX_WAIT_S)] = 0,
) -> NoticeOut:
    request.state.debug_detail = f"panel notice {notice_id[:8]} wait={wait:g}"
    return await read(request, notice_id, wait, device_id)


# ---- the panel ---------------------------------------------------------------------------


def _panel_only(principal: PrincipalInfo) -> None:
    # The owner's cookie also satisfies `PanelDep`; the owner has no notices to collect.
    if principal.kind != "device_key":
        raise HTTPException(status_code=403, detail="a panel's own key is required")


def _stream(body: bytes) -> Response:
    return Response(content=body, media_type=MEDIA_TYPE, headers={"Cache-Control": "no-store"})


@panel_router.get("/next", response_model=None)
async def next_notice(principal: PanelDep, request: Request) -> Response:
    """The oldest notice waiting for this panel as a `JN1` stream, marked delivered — which
    starts its answer window — or 204. The panel calls it when its debug pickup says one
    waits (`X-Notice`), so an idle panel costs nothing more than the pickup it already makes."""
    _panel_only(principal)
    relay.mark_seen(principal.id, None)
    body = await service(request).stream_for(ctx_for(principal), None)
    if body is None:
        return Response(status_code=204)
    log.info("panel.notice_pickup", device=principal.id, bytes=len(body))
    return _stream(body)


@panel_router.get("/{notice_id}/body", response_model=None)
async def notice_body(notice_id: str, principal: PanelDep, request: Request) -> Response:
    """The same stream again for a notice this panel already collected — the retry after a
    download torn by the radio. 404 for anything else."""
    _panel_only(principal)
    relay.mark_seen(principal.id, None)
    try:
        body = await service(request).stream_for(ctx_for(principal), _uuid(notice_id))
    except ValueError:
        body = None
    if body is None:
        raise HTTPException(status_code=404, detail={"reason": "no_such_notice"})
    return _stream(body)


def _uuid(value: str) -> str:
    return str(uuid.UUID(value))


class StateIn(BaseModel):
    #: shown (on the glass), answered (with `choice`), dismissed (tapped away or BOOT),
    #: expired (its `hide_after_s` ran out).
    state: Literal["shown", "answered", "dismissed", "expired"]
    #: The tapped button's index, 0-based, in the order the CHOICE records came.
    choice: int | None = Field(default=None, ge=0, le=N.CHOICES_MAX - 1)


@panel_router.post("/{notice_id}/state", status_code=204, response_model=None)
async def notice_state(
    notice_id: str, body: StateIn, principal: PanelDep, request: Request
) -> Response:
    """The panel moves its own notice along. 204; 404 for anything not this panel's live
    notice (a sibling's, a finished one, one past its deadline — RLS hides all alike); 409 for
    a move out of order; 422 for a choice that is not one of the notice's."""
    _panel_only(principal)
    relay.mark_seen(principal.id, None)
    try:
        await service(request).panel_state(ctx_for(principal), notice_id, body.state, body.choice)
    except N.NoticeRefused as exc:
        raise _refuse(exc) from None
    return Response(status_code=204)
