"""The owner's desk-panel alarms: set in the PWA, synced to the panel, switched on the glass.

docs/mocks/jpanel-c6/README.md (owner decisions): alarms are created and edited in the PWA
(jpanel → Panels → Jeff's) and only switched on and off on the panel, which keeps its own copy
and rings from its own clock — so an alarm still sounds with the box unreachable. Table and
RLS: migration 0235.

Two doors:

- **The owner's** (`/endpoint/panels/{device_id}/alarms`): list, add, change, delete. A
  `display` panel only: the twins' S3 panels have no alarm code, and are refused (409) rather
  than given rows nothing would ever ring.
- **The panel's** (`GET /endpoint/alarms`, `PUT /endpoint/alarms/{id}`), by its device key. The
  same `display` gate, read under the panel's OWN RLS context, so the role comes from the row
  the flash wrote and never from the request. The panel polls on its manifest pass with the
  etag it holds; an unchanged list is a 304 with no body. The PUT carries `{on}` only — the
  switch on the glass — and the table's trigger refuses any other column from a device key.

Rules: every query on an RLS-scoped session (#3); no LLM here.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import OwnerDep, PanelDep
from jbrain.api.notes import ctx_for
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext, panel_context, scoped_session

log = structlog.get_logger()

router = APIRouter(prefix="/endpoint", tags=["endpoint"])

#: The panel's `ALARM_MAX`; the table's trigger holds the same number.
ALARMS_MAX = 8
#: The panel's `ALARM_LABEL_BYTES`. Printable ASCII only: it is what the panel's fonts draw.
LABEL_MAX = 24
Sound = Literal["chime", "beeps", "rise"]
SOUNDS: tuple[Sound, ...] = ("chime", "beeps", "rise")


class AlarmIn(BaseModel):
    """One alarm as the owner sets it. `at` is minutes past midnight in the panel's own zone;
    `days` bit 0 is Monday … bit 6 Sunday, and 0 rings once at the next occurrence."""

    at: int = Field(ge=0, le=1439)
    days: int = Field(default=0, ge=0, le=127)
    on: bool = True
    label: str = ""
    sound: Sound = "chime"

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) > LABEL_MAX or any(not (" " <= c <= "~") for c in v):
            raise ValueError(
                f"letters, digits and punctuation the panel can draw, {LABEL_MAX} at most"
            )
        return v


class Alarm(AlarmIn):
    id: int


class Alarms(BaseModel):
    """The list, oldest-first by time of day, and the version the panel compares. The owner's
    copy also says which version the panel last fetched and how long ago, so the PWA can say
    "On the panel" only when it is."""

    etag: str
    alarms: list[Alarm]
    max: int = ALARMS_MAX
    panel_etag: str | None = None
    panel_seen_s: int | None = None


#: subject_id -> (the etag the panel last fetched or confirmed, monotonic time). In memory, like
#: the debug relay's `_seen`: the panel re-asserts it every minute, so a box restart costs one
#: poll of "waiting for the panel" and nothing else.
_held: dict[str, tuple[str, float]] = {}


def _panel_holds(subject_id: str, tag: str) -> None:
    _held[subject_id] = (tag, time.monotonic())


class AlarmSwitch(BaseModel):
    on: bool


class Switched(BaseModel):
    id: int
    on: bool
    etag: str


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


_COLUMNS = "id, minute, days, enabled, label, sound"


def _row(r: object) -> Alarm:
    t = cast(tuple[int, int, int, bool, str, str], r)
    return Alarm(id=t[0], at=t[1], days=t[2], on=t[3], label=t[4], sound=cast(Sound, t[5]))


def etag_of(alarms: list[Alarm]) -> str:
    """A digest of exactly what the panel stores, so any change it could see moves the tag and
    nothing it cannot see does. Sixteen hex characters: a version, not a secret."""
    body = json.dumps([a.model_dump() for a in alarms], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:16]


async def _list(s: AsyncSession, subject_id: str) -> list[Alarm]:
    rows = (
        await s.execute(
            text(
                f"SELECT {_COLUMNS} FROM app.endpoint_panel_alarm"
                " WHERE subject_id = CAST(:sid AS uuid) ORDER BY minute, id"
            ),
            {"sid": subject_id},
        )
    ).all()
    return [_row(r) for r in rows]


# ---- the owner's door ------------------------------------------------------------------------


async def _display_subject(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, device_id: str
) -> str:
    """The subject of a live `display` panel key, or 404; a pet panel is 409 — it has no alarms."""
    try:
        uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such panel") from None
    async with scoped_session(maker, ctx) as s:
        row = (
            await s.execute(
                text(
                    """
                    SELECT s.id::text, s.device_role FROM app.principals p
                    JOIN app.subjects s ON s.id = p.subject_id
                    WHERE p.id = CAST(:dev AS uuid) AND p.kind = 'device_key'
                      AND s.device_role IS NOT NULL
                    """
                ),
                {"dev": device_id},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="no such panel")
    if row[1] != "display":
        raise HTTPException(status_code=409, detail="alarms are on the desk panel only")
    return str(row[0])


async def list_alarms(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, device_id: str
) -> Alarms:
    sid = await _display_subject(maker, ctx, device_id)
    async with scoped_session(maker, ctx) as s:
        alarms = await _list(s, sid)
    held = _held.get(sid)
    return Alarms(
        etag=etag_of(alarms),
        alarms=alarms,
        panel_etag=held[0] if held else None,
        panel_seen_s=int(time.monotonic() - held[1]) if held else None,
    )


async def add_alarm(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, device_id: str, body: AlarmIn
) -> Alarm:
    sid = await _display_subject(maker, ctx, device_id)
    async with scoped_session(maker, ctx) as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.endpoint_panel_alarm"
                    " WHERE subject_id = CAST(:sid AS uuid)"
                ),
                {"sid": sid},
            )
        ).scalar_one()
        if n >= ALARMS_MAX:
            raise HTTPException(status_code=409, detail=f"the panel holds {ALARMS_MAX} alarms")
        row = (
            await s.execute(
                text(
                    "INSERT INTO app.endpoint_panel_alarm"
                    " (subject_id, minute, days, enabled, label, sound)"
                    " VALUES (CAST(:sid AS uuid), :at, :days, :on, :label, :sound)"
                    f" RETURNING {_COLUMNS}"
                ),
                {"sid": sid, **body.model_dump()},
            )
        ).one()
        await s.commit()
    log.info("panel_alarms.added", device=device_id, at=body.at, days=body.days)
    return _row(row)


async def change_alarm(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    device_id: str,
    alarm_id: int,
    body: AlarmIn,
) -> Alarm:
    sid = await _display_subject(maker, ctx, device_id)
    async with scoped_session(maker, ctx) as s:
        row = (
            await s.execute(
                text(
                    "UPDATE app.endpoint_panel_alarm SET minute = :at, days = :days,"
                    " enabled = :on, label = :label, sound = :sound"
                    " WHERE id = :id AND subject_id = CAST(:sid AS uuid)"
                    f" RETURNING {_COLUMNS}"
                ),
                {"id": alarm_id, "sid": sid, **body.model_dump()},
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="no such alarm")
        await s.commit()
    return _row(row)


async def delete_alarm(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, device_id: str, alarm_id: int
) -> None:
    sid = await _display_subject(maker, ctx, device_id)
    async with scoped_session(maker, ctx) as s:
        gone = (
            await s.execute(
                text(
                    "DELETE FROM app.endpoint_panel_alarm"
                    " WHERE id = :id AND subject_id = CAST(:sid AS uuid) RETURNING id"
                ),
                {"id": alarm_id, "sid": sid},
            )
        ).first()
        if gone is None:
            raise HTTPException(status_code=404, detail="no such alarm")
        await s.commit()
    log.info("panel_alarms.deleted", device=device_id, alarm=alarm_id)


@router.get("/panels/{device_id}/alarms")
async def get_alarms(device_id: str, owner: OwnerDep, request: Request) -> Alarms:
    """The desk panel's alarms. Owner only."""
    return await list_alarms(_maker(request), ctx_for(owner), device_id)


@router.post("/panels/{device_id}/alarms", status_code=201)
async def post_alarm(device_id: str, owner: OwnerDep, request: Request, body: AlarmIn) -> Alarm:
    """A new alarm; 409 once the panel holds eight. Owner only."""
    return await add_alarm(_maker(request), ctx_for(owner), device_id, body)


@router.put("/panels/{device_id}/alarms/{alarm_id}")
async def put_alarm(
    device_id: str, alarm_id: int, owner: OwnerDep, request: Request, body: AlarmIn
) -> Alarm:
    """The whole alarm, replaced (time, days, label, sound, on). Owner only."""
    return await change_alarm(_maker(request), ctx_for(owner), device_id, alarm_id, body)


@router.delete("/panels/{device_id}/alarms/{alarm_id}", status_code=204)
async def remove_alarm(device_id: str, alarm_id: int, owner: OwnerDep, request: Request) -> None:
    await delete_alarm(_maker(request), ctx_for(owner), device_id, alarm_id)


# ---- the panel's door -----------------------------------------------------------------------


async def _own_display(maker: async_sessionmaker[AsyncSession], principal: PrincipalInfo) -> str:
    """The calling panel's subject when it was flashed as a `display`, else 403. The twins' S3
    panels (`jpet`) have no alarm code, so they are kept off these routes rather than served
    a list. Read under the panel's own context: only its own subject row is visible."""
    sid = principal.subject_id
    if principal.kind != "device_key" or not sid:
        raise HTTPException(status_code=403, detail="a panel's own route")
    async with scoped_session(maker, panel_context(principal.id, sid)) as s:
        role = (
            await s.execute(
                text("SELECT device_role FROM app.subjects WHERE id = CAST(:sid AS uuid)"),
                {"sid": sid},
            )
        ).scalar_one_or_none()
    if role != "display":
        raise HTTPException(status_code=403, detail="not a display panel")
    return sid


def _matches(tag: str, held: str | None) -> bool:
    """`If-None-Match` as a header (`"abc"`, `W/"abc"` or a list) or the panel's `?etag=`."""
    if not held:
        return False
    for part in held.split(","):
        p = part.strip()
        if p.startswith("W/"):
            p = p[2:]
        if p.strip('"') == tag:
            return True
    return False


@router.get("/alarms", response_model=None)
async def panel_alarms(
    principal: PanelDep,
    request: Request,
    etag: str | None = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Alarms | Response:
    """The calling desk panel's alarms. 304 with no body when `etag` (or `If-None-Match`)
    is the current one, so the minute poll costs a status line."""
    maker = _maker(request)
    sid = await _own_display(maker, principal)
    async with scoped_session(maker, panel_context(principal.id, sid)) as s:
        alarms = await _list(s, sid)
    tag = etag_of(alarms)
    _panel_holds(sid, tag)
    if _matches(tag, etag) or _matches(tag, if_none_match):
        return Response(status_code=304, headers={"ETag": f'"{tag}"'})
    return Alarms(etag=tag, alarms=alarms)


@router.put("/alarms/{alarm_id}")
async def panel_switch(
    alarm_id: int, principal: PanelDep, request: Request, body: AlarmSwitch
) -> Switched:
    """The switch on the glass: one alarm on or off, the panel's own only. A sibling's id, or
    one the owner has deleted since, is 404 — the panel drops that queued write."""
    maker = _maker(request)
    sid = await _own_display(maker, principal)
    async with scoped_session(maker, panel_context(principal.id, sid)) as s:
        got = (
            await s.execute(
                text(
                    "UPDATE app.endpoint_panel_alarm SET enabled = :on WHERE id = :id RETURNING id"
                ),
                {"id": alarm_id, "on": body.on},
            )
        ).first()
        if got is None:
            raise HTTPException(status_code=404, detail="no such alarm")
        alarms = await _list(s, sid)
        await s.commit()
    log.info("panel_alarms.switched", device=principal.id, alarm=alarm_id, on=body.on)
    return Switched(id=alarm_id, on=body.on, etag=etag_of(alarms))
