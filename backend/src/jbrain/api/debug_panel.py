"""`/api/debug/panel/*` — ask a live panel something, through the box (JPANEL_C6_PLAN.md §4).

The owner: *"a very good debugging API so that if I give you a token you can pull current
state or whatever data you need from the device."* A panel cannot be reached directly, so
a debug token queues a command here, the panel collects it on its next pickup
(`api/panel_debug.py`) and posts the answer, and the token holder long-polls for it.

Gated by `DebugDep` like the rest of `/api/debug`, and mounted only when debug access is
enabled. The queue runs under an owner context — the table's owner policy is the one that
lets anything insert — with the token's label recorded on every row and every log line
(`debug.panel_command`), so what a session did to a panel is on the record.

`owner_router` is the PWA's half (Ops → Panels): the owner's own cookie may ask for the two
looks — a `snapshot` or a `screenshot` — and nothing else, through the same queue and the
same refusals. It is mounted whatever the debug flag says, because looking at a panel is an
operator's question and the owner has no terminal to turn the flag on (CLAUDE.md #10); what a
token can do beyond looking stays behind the token.

Refused at the door, never queued for later: an unknown command, an argument the panel's
flat parser would refuse, a destructive one without `confirm`, and a panel that has not
picked up in 30 s (§2's offline rule). A request is a question for the panel as it is now.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal, cast

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import DebugDep, OwnerDep
from jbrain.api.endpoint import panel_display_name
from jbrain.blob_refs import blob_referenced
from jbrain.db.session import SessionContext, scoped_session
from jbrain.jpanel import debug_relay as relay
from jbrain.jpanel import notice
from jbrain.storage import BlobStore

log = structlog.get_logger()

router = APIRouter(prefix="/debug/panel")
owner_router = APIRouter(prefix="/panel/look", tags=["panel"])

# The debug console's fixed synthetic owner identity (the same one `api/debug.py` uses), so
# the audit trail reads as the console rather than as a person.
_OWNER_CTX = SessionContext(principal_id="debug-console", principal_kind="owner")

#: How often a long poll re-reads the row. Half a second against a panel that answers in
#: seconds; a debug session is one person, so the read load is nothing.
_POLL_S = 0.5


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


def _blobs(request: Request) -> BlobStore:
    return cast(BlobStore, request.app.state.blob_store)


def _refuse(exc: relay.CommandRefused) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"reason": exc.reason, **exc.extra})


async def sweep(request: Request) -> int:
    """Expire what was never collected or answered, and delete what finished long ago.

    Lazy — run at the top of every owner route — because nothing else reads this table:
    a row nobody asks about costs a few hundred bytes until the next debug session. The
    screenshot blobs go with their rows, each only once `blob_refs` says nothing else on
    the box holds the same bytes."""
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        await session.execute(
            text(
                """
                UPDATE app.panel_debug_request
                SET state = 'expired', result_at = expires_at
                WHERE state IN ('queued', 'sent') AND expires_at <= now()
                """
            )
        )
        gone = (
            await session.execute(
                text(
                    """
                    DELETE FROM app.panel_debug_request
                    WHERE state IN ('done', 'expired')
                      AND result_at <= now() - make_interval(secs => :ttl)
                    RETURNING png_sha256
                    """
                ),
                {"ttl": relay.RESULT_TTL_S},
            )
        ).all()
        shas = {str(r[0]) for r in gone if r[0]}
        free = [sha for sha in shas if not await blob_referenced(session, sha)]
        await session.commit()
    for sha in free:
        await _blobs(request).delete(sha)
    return len(gone)


async def _panel(request: Request, device_id: str) -> str:
    """The panel's name, or 404 for anything that is not a live panel's key."""
    try:
        uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such panel") from None
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        label = (
            await session.execute(
                text(
                    """
                    SELECT s.display_name
                    FROM app.principals p
                    JOIN app.subjects s ON s.id = p.subject_id
                    WHERE p.id = CAST(:dev AS uuid) AND p.kind = 'device_key'
                      AND p.revoked_at IS NULL AND s.device_role IS NOT NULL
                    """
                ),
                {"dev": device_id},
            )
        ).scalar_one_or_none()
    if label is None:
        raise HTTPException(status_code=404, detail="no such panel")
    return panel_display_name(str(label))


class PanelOut(BaseModel):
    device_id: str
    name: str
    #: The board the panel named on its last pickup; None when it has not picked up since
    #: this box started (an S3 panel never does — it has no debug surface).
    board: str | None
    last_seen_s: int | None
    online: bool
    #: What its last `help` published; None until one has been asked.
    commands: list[dict[str, Any]] | None
    #: The notice switches and night window its last pickup reported (§2); None when its
    #: firmware reports none, which a notice is refused for as `no_notice_surface`.
    notices: dict[str, Any] | None = None


@router.get("")
async def list_panels(request: Request, _p: DebugDep) -> list[PanelOut]:
    """Every live panel and whether it is picking up — so a client can find "the C6" by
    board without knowing its id, and see why a command would be refused as offline."""
    request.state.debug_detail = "panel list"
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT ON (s.display_name) p.id::text, s.display_name
                    FROM app.principals p
                    JOIN app.subjects s ON s.id = p.subject_id
                    WHERE p.kind = 'device_key' AND p.revoked_at IS NULL
                      AND s.device_role IS NOT NULL
                    ORDER BY s.display_name, p.created_at DESC
                    """
                )
            )
        ).all()
    out: list[PanelOut] = []
    for device_id, label in rows:
        seen = relay.last_seen(str(device_id))
        pub = relay.published_commands(str(device_id))
        out.append(
            PanelOut(
                device_id=str(device_id),
                name=panel_display_name(str(label)),
                board=seen[1] if seen else None,
                last_seen_s=int(seen[0]) if seen else None,
                online=seen is not None and seen[0] <= relay.OFFLINE_AFTER_S,
                commands=[c.__dict__ for c in pub] if pub is not None else None,
                notices=prefs.as_dict() if (prefs := notice.prefs_for(str(device_id))) else None,
            )
        )
    return out


class CommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=15)
    args: dict[str, Any] = Field(default_factory=dict, max_length=8)
    confirm: bool = False


class CommandQueued(BaseModel):
    request_id: str
    state: str
    expires_in_s: int


async def _queue(
    request: Request,
    device_id: str,
    body: CommandIn,
    *,
    by_label: str,
    by_id: str,
) -> CommandQueued:
    name = await _panel(request, device_id)
    try:
        info = relay.check_command(device_id, body.command)
        args = relay.check_args(body.args)
    except relay.CommandRefused as exc:
        raise _refuse(exc) from None
    if info.destructive and not body.confirm:
        raise HTTPException(
            status_code=409,
            detail={"reason": "needs_confirm", "command": info.name},
        )
    seen = relay.last_seen(device_id)
    if seen is None or seen[0] > relay.OFFLINE_AFTER_S:
        raise HTTPException(
            status_code=503,
            detail={"reason": "offline", "last_seen_s": int(seen[0]) if seen else None},
        )
    if seen[1] != relay.DEBUG_BOARD:
        raise HTTPException(
            status_code=409, detail={"reason": "no_debug_surface", "board": seen[1]}
        )
    await sweep(request)
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        waiting = (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.panel_debug_request"
                    " WHERE device_id = CAST(:dev AS uuid) AND state = 'queued'"
                ),
                {"dev": device_id},
            )
        ).scalar_one()
        if int(waiting) >= relay.MAX_QUEUED:
            raise HTTPException(status_code=429, detail={"reason": "busy", "queued": int(waiting)})
        request_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO app.panel_debug_request
                        (device_id, command, args, confirm, created_by, expires_at)
                    VALUES (CAST(:dev AS uuid), :cmd, CAST(:args AS jsonb), :confirm, :by,
                            now() + make_interval(secs => :window))
                    RETURNING id::text
                    """
                ),
                {
                    "dev": device_id,
                    "cmd": info.name,
                    "args": json.dumps(args),
                    "confirm": body.confirm,
                    "by": f"{by_label} ({by_id})"[:200],
                    "window": relay.PICKUP_WINDOW_S,
                },
            )
        ).scalar_one()
        await session.commit()
    log.info(
        "debug.panel_command",
        token=by_label,
        token_id=by_id,
        device=device_id,
        panel=name,
        cmd=info.name,
        args=args,
        confirm=body.confirm,
        request_id=request_id,
    )
    return CommandQueued(
        request_id=str(request_id), state="queued", expires_in_s=relay.PICKUP_WINDOW_S
    )


@router.post("/{device_id}/command", status_code=202)
async def queue_command(
    device_id: str, body: CommandIn, request: Request, principal: DebugDep
) -> CommandQueued:
    """Queue one command for a panel; poll `result/{request_id}` for the answer.

    202 `{request_id}`. Refusals are `{"detail": {"reason": …}}`: 422 `unknown_command` /
    `bad_arg`, 409 `needs_confirm` (a destructive command without `confirm: true`), 409
    `unavailable` (this panel's `help` says its firmware does not bind it), 409
    `no_debug_surface` (the panel is not a C6), 429 `busy` (too many already waiting), 503
    `offline` with `last_seen_s` (null: not seen since the box started)."""
    request.state.debug_detail = f"panel {device_id[:8]} {body.command}"
    return await _queue(request, device_id, body, by_label=principal.label, by_id=principal.id)


class ResultOut(BaseModel):
    request_id: str
    device_id: str
    command: str
    args: dict[str, Any]
    #: queued -> sent -> done, or expired. Anything but done/expired: poll again.
    state: str
    created_at: str
    picked_up_at: str | None
    result_at: str | None
    #: `dbg_cmd_run`'s answer as the panel sent it, or the box's summary of a screenshot.
    result: dict[str, Any] | None
    #: The decoded screenshot, when this request produced one.
    screenshot: str | None


def _debug_png(device_id: str, request_id: str) -> str:
    return f"/api/debug/panel/{device_id}/screenshot/{request_id}.png"


def _owner_png(device_id: str, request_id: str) -> str:
    return f"/api/panel/look/{device_id}/{request_id}/screenshot.png"


async def _read_result(
    request: Request, device_id: str, request_id: str, png_url: Callable[[str, str], str]
) -> ResultOut | None:
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id::text, device_id::text, command, args,
                           CASE WHEN state IN ('queued', 'sent') AND expires_at <= now()
                                THEN 'expired' ELSE state END,
                           created_at, picked_up_at, result_at, result, png_sha256
                    FROM app.panel_debug_request
                    WHERE id = CAST(:id AS uuid) AND device_id = CAST(:dev AS uuid)
                    """
                ),
                {"id": request_id, "dev": device_id},
            )
        ).first()
    if row is None:
        return None
    return ResultOut(
        request_id=str(row[0]),
        device_id=str(row[1]),
        command=str(row[2]),
        args=dict(row[3] or {}),
        state=str(row[4]),
        created_at=row[5].isoformat(),
        picked_up_at=row[6].isoformat() if row[6] else None,
        result_at=row[7].isoformat() if row[7] else None,
        result=dict(row[8]) if row[8] is not None else None,
        screenshot=png_url(str(row[1]), str(row[0])) if row[9] else None,
    )


def _ids(device_id: str, request_id: str) -> None:
    try:
        uuid.UUID(device_id)
        uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such request") from None


async def _await_result(
    request: Request,
    device_id: str,
    request_id: str,
    wait: float,
    png_url: Callable[[str, str], str],
) -> ResultOut:
    _ids(device_id, request_id)
    await sweep(request)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        out = await _read_result(request, device_id, request_id, png_url)
        if out is None:
            raise HTTPException(status_code=404, detail="no such request")
        if out.state in ("done", "expired") or loop.time() >= deadline:
            return out
        await asyncio.sleep(min(_POLL_S, max(0.0, deadline - loop.time())))


@router.get("/{device_id}/result/{request_id}")
async def get_result(
    device_id: str,
    request_id: str,
    request: Request,
    _p: DebugDep,
    wait: Annotated[float, Query(ge=0, le=relay.MAX_WAIT_S)] = 0,
) -> ResultOut:
    """The request's state and, once done, its result. `wait` (≤ 25 s) holds the call open
    until it is done or expired; a call that returns `queued` or `sent` is simply asked
    again. 404 once swept (fifteen minutes after it finished)."""
    request.state.debug_detail = f"panel result {request_id[:8]} wait={wait:g}"
    return await _await_result(request, device_id, request_id, wait, _debug_png)


async def _png(request: Request, device_id: str, request_id: str) -> Response:
    _ids(device_id, request_id)
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        sha = (
            await session.execute(
                text(
                    "SELECT png_sha256 FROM app.panel_debug_request"
                    " WHERE id = CAST(:id AS uuid) AND device_id = CAST(:dev AS uuid)"
                ),
                {"id": request_id, "dev": device_id},
            )
        ).scalar_one_or_none()
    if not sha:
        raise HTTPException(status_code=404, detail="no screenshot for this request")
    try:
        png = await _blobs(request).get(str(sha))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="screenshot no longer stored") from None
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/{device_id}/screenshot/{request_id}.png")
async def get_screenshot(
    device_id: str, request_id: str, request: Request, _p: DebugDep
) -> Response:
    """The decoded PNG of a `screenshot` request, while its row lives."""
    request.state.debug_detail = f"panel screenshot {request_id[:8]}"
    return await _png(request, device_id, request_id)


# ---- the owner's two looks, from Ops → Panels --------------------------------------------


class LookIn(BaseModel):
    #: Only what reads the panel. A press in the PWA that could reboot it or drive its
    #: screen is a different decision, and today that belongs to the token.
    what: Literal["snapshot", "screenshot"]


@owner_router.post("/{device_id}", status_code=202)
async def owner_look(
    device_id: str, body: LookIn, request: Request, owner: OwnerDep
) -> CommandQueued:
    """Ask a panel for a snapshot or a screenshot; read it from `{request_id}`.

    The debug route's refusals, from the same checks: 503 `offline`, 409
    `no_debug_surface` / `unavailable`, 429 `busy`, 404 for no such panel."""
    return await _queue(
        request, device_id, CommandIn(command=body.what), by_label="owner PWA", by_id=owner.id
    )


@owner_router.get("/{device_id}/{request_id}")
async def owner_look_result(
    device_id: str,
    request_id: str,
    request: Request,
    _o: OwnerDep,
    wait: Annotated[float, Query(ge=0, le=relay.MAX_WAIT_S)] = 0,
) -> ResultOut:
    """As `result/{request_id}`, with the screenshot's URL on the owner's own route."""
    return await _await_result(request, device_id, request_id, wait, _owner_png)


@owner_router.get("/{device_id}/{request_id}/screenshot.png")
async def owner_look_png(
    device_id: str, request_id: str, request: Request, _o: OwnerDep
) -> Response:
    """The decoded PNG of a look, while its row lives (fifteen minutes after it finished)."""
    return await _png(request, device_id, request_id)
