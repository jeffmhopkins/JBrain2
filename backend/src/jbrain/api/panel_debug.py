"""`/api/endpoint/debug/*` — the panel's half of the debug relay (JPANEL_C6_PLAN.md §4).

Two routes, both `PanelDep` (the panel's own device key as a bearer token):

- `GET /endpoint/debug/next?board=` — collect one pending request, or 204. §4 has this
  ride the ~3 s settings poll, but the C6 has no settings poll yet (it polls the manifest
  every 60 s, far too slow for a debug session), so it is a small route of its own that
  0.0.9 calls on a short interval. Calling it is also the panel's proof of life: a command
  is refused as `offline` for a panel that has not called it in 30 s.
- `POST /endpoint/debug/{request_id}` — the answer: `dbg_cmd_run`'s JSON, or for a
  `screenshot` the raw `JPS1` stream, which the box decodes to a PNG kept in the blob
  store (CLAUDE.md #2).

Every read and write runs under the PANEL's own context, so the table's policies — its own
pending rows only, never a sibling's, never an insert — are what bound these routes, not
the SQL in them (CLAUDE.md #3, migration 0231).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, cast

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api import panel_messages
from jbrain.api.deps import PanelDep
from jbrain.api.endpoint import DEFAULT_BOARD, BoardId
from jbrain.api.notes import ctx_for
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import scoped_session
from jbrain.jpanel import debug_relay as relay
from jbrain.jpanel import notice
from jbrain.jpanel.notice_service import queued_count
from jbrain.storage import BlobStore

log = structlog.get_logger()

router = APIRouter(prefix="/endpoint/debug", tags=["endpoint"])


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


def _panel_only(principal: PrincipalInfo) -> None:
    # The owner's cookie also satisfies `PanelDep`; the owner has no requests to collect
    # or answer, and acting as a panel from the PWA is not a thing this should allow.
    if principal.kind != "device_key":
        raise HTTPException(status_code=403, detail="a panel's own key is required")


@router.get("/next", response_model=None)
async def next_request(
    principal: PanelDep, request: Request, board: BoardId = DEFAULT_BOARD
) -> Response | dict[str, Any]:
    """The oldest request waiting for this panel, marked `sent`, or 204.

    The query may also carry the panel's notice settings — `na`, `nq`, `nc` (accept, refuse
    at night, chime: 0/1), `nf`, `nt` (the night window, minutes after local midnight) and
    `tz` (the panel's zone index) — and either answer carries `X-Notice: <n>` when n notices
    wait for it (JPANEL_C6_PLAN.md §2). For the owner's desk panel (the talk's gate) either answer
    also carries `X-Jpanel-Post: <tag>`, which moves whenever his voice post does
    (`panel_messages.post_tag`, 0.0.16): the panel fetches its person threads only then.

    200 carries exactly the flat object `dbg_cmd_run` takes. Collecting it starts the
    answer window (`relay.ANSWER_WINDOW_S`); a request not collected within
    `relay.PICKUP_WINDOW_S` of being asked is never delivered."""
    _panel_only(principal)
    relay.mark_seen(principal.id, board)
    # The pickup also carries the panel's notice switches and night window (C3b), so the box
    # can refuse a notice at send time from what the panel said seconds ago (notice.py).
    notice.record_prefs(principal.id, notice.parse_prefs(dict(request.query_params)))
    async with scoped_session(_maker(request), ctx_for(principal)) as session:
        waiting = await queued_count(session)
        row = (
            await session.execute(
                text(
                    """
                    UPDATE app.panel_debug_request SET state = 'sent'
                    WHERE id = (
                        SELECT id FROM app.panel_debug_request
                        WHERE device_id = CAST(:me AS uuid) AND state = 'queued'
                          AND expires_at > now()
                        ORDER BY created_at
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id::text, command, args, confirm
                    """
                ),
                {"me": principal.id},
            )
        ).first()
        await session.commit()
    # `X-Notice: <n>` when notices wait: the panel then collects them from
    # `/endpoint/notice/next` (api/panel_notice.py), so an idle pickup stays one request.
    hint = {"X-Notice": str(waiting)} if waiting else {}
    tag = await panel_messages.pickup_tag(_maker(request), principal)
    if tag:
        hint[panel_messages.POST_HEADER] = tag
    if row is None:
        return Response(status_code=204, headers=hint)
    log.info("debug.panel_pickup", device=principal.id, request_id=row[0], cmd=row[1])
    return JSONResponse(
        relay.panel_request(str(row[0]), str(row[1]), dict(row[2] or {}), bool(row[3])),
        headers=hint,
    )


class _TooLarge(Exception):
    pass


async def _read_capped(request: Request, cap: int) -> bytes:
    """The body, refused once it passes `cap` — counted as it arrives, so a chunked upload
    with no Content-Length is bounded too."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise _TooLarge
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > cap:
            raise _TooLarge
    return bytes(buf)


def _too_large(cap: int) -> JSONResponse:
    return JSONResponse(status_code=413, content={"detail": {"reason": "too_large", "max": cap}})


@router.post("/{request_id}", status_code=204, response_model=None)
async def post_result(request_id: str, principal: PanelDep, request: Request) -> Response:
    """The panel's answer to one request it collected.

    `Content-Type: application/json` — `dbg_cmd_run`'s output, ≤ 64 KiB; its `id`, when
    present, must be this request's. For `screenshot`, `application/octet-stream` — the
    `JPS1` stream, ≤ 1 MiB, chunked is fine. A stream that does not decode is recorded as
    a failed result (`ok: false, err: "bad_stream"`) and answered 422, so the person
    waiting learns now rather than at the deadline.

    404 for anything that is not this panel's request in `sent`: unknown, a sibling's,
    already answered, or past its deadline — RLS hides all four the same way."""
    _panel_only(principal)
    try:
        uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such pending request") from None
    relay.mark_seen(principal.id, None)
    ctx = ctx_for(principal)
    async with scoped_session(_maker(request), ctx) as session:
        command = (
            await session.execute(
                text(
                    "SELECT command FROM app.panel_debug_request"
                    " WHERE id = CAST(:id AS uuid) AND state = 'sent'"
                ),
                {"id": request_id},
            )
        ).scalar_one_or_none()
    if command is None:
        raise HTTPException(status_code=404, detail="no such pending request")

    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    png_sha: str | None = None
    status = 204
    if ctype == "application/json":
        try:
            raw = await _read_capped(request, relay.MAX_RESULT_JSON)
        except _TooLarge:
            return _too_large(relay.MAX_RESULT_JSON)
        try:
            result = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail="result is not JSON") from None
        if not isinstance(result, dict):
            raise HTTPException(status_code=422, detail="result must be a JSON object")
        if result.get("id") not in (None, "", request_id):
            raise HTTPException(status_code=422, detail="result id does not match the request")
        if command == "help":
            relay.record_help(principal.id, result)
    elif command == "screenshot" and ctype == "application/octet-stream":
        try:
            stream = await _read_capped(request, relay.MAX_SHOT_STREAM)
        except _TooLarge:
            return _too_large(relay.MAX_SHOT_STREAM)
        try:
            png, shot = await asyncio.to_thread(relay.png_from_jps1, stream)
        except relay.ShotError as exc:
            result = {
                "id": request_id,
                "cmd": command,
                "ok": False,
                "err": "bad_stream",
                "why": str(exc),
                "bytes": len(stream),
            }
            status = 422
        else:
            blobs = cast(BlobStore, request.app.state.blob_store)
            png_sha = await blobs.put(png)
            result = {
                "id": request_id,
                "cmd": command,
                "ok": True,
                "data": {
                    "w": shot.width,
                    "h": shot.height,
                    "stripes": shot.stripes,
                    "bytes": len(stream),
                    "png_bytes": len(png),
                },
            }
    else:
        raise HTTPException(
            status_code=415,
            detail="application/json, or application/octet-stream for a screenshot",
        )

    async with scoped_session(_maker(request), ctx) as session:
        done = (
            await session.execute(
                text(
                    """
                    UPDATE app.panel_debug_request
                    SET state = 'done', result = CAST(:result AS jsonb), png_sha256 = :png
                    WHERE id = CAST(:id AS uuid) AND state = 'sent'
                    RETURNING id
                    """
                ),
                {"id": request_id, "result": json.dumps(result), "png": png_sha},
            )
        ).first()
        await session.commit()
    if done is None:
        # Expired between the check and the write. The PNG, if any, is an orphan the size
        # of one screen; the blob store has no refcount to drop it by safely (blob_refs).
        raise HTTPException(status_code=404, detail="no such pending request")
    log.info(
        "debug.panel_result",
        device=principal.id,
        request_id=request_id,
        cmd=command,
        ok=result.get("ok"),
        png=bool(png_sha),
    )
    if status == 422:
        return JSONResponse(
            status_code=422, content={"detail": {"reason": "bad_stream", "why": result["why"]}}
        )
    return Response(status_code=204)
