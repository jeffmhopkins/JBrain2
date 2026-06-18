"""The live location WebSocket (JBrain360 M3b).

A separate router from the owner-only REST surface because WebSocket auth reads the
session cookie off the handshake, not via a `Request` dependency. Owner-only for
now (the owner sees every device live); scope-filtered *member* connections arrive
with M4's session bridge. The browser never holds MQTT creds — it receives a
server-filtered stream off the in-process `LiveBroadcaster` (plan B4).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from jbrain.auth import service
from jbrain.auth.service import AuthRepo, PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.locations.live import LiveBroadcaster, LiveFix

router = APIRouter()
log = structlog.get_logger()


class AuditSink(Protocol):
    async def record_view(
        self,
        ctx: SessionContext,
        *,
        viewer_principal_id: str,
        viewer_subject_id: str,
        target_subject_id: str,
        path: str,
    ) -> None: ...


async def authenticated_owner(websocket: WebSocket) -> PrincipalInfo | None:
    """The owner principal from the session cookie on the WS handshake, or None."""
    repo = cast(AuthRepo, websocket.app.state.auth_repo)
    settings = cast(Settings, websocket.app.state.settings)
    token = websocket.cookies.get(settings.session_cookie, "")
    principal = await service.authenticate(repo, token)
    return principal if principal is not None and principal.kind == "owner" else None


def live_out(fix: LiveFix) -> dict[str, Any]:
    return {
        "subject_id": fix.subject_id,
        "lat": fix.latitude,
        "lon": fix.longitude,
        "accuracy_m": fix.accuracy_m,
        "battery_pct": fix.battery_pct,
        "captured_at": fix.captured_at.isoformat(),
    }


async def deliver_fix(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    repo: AuditSink,
    ctx: SessionContext,
    principal: PrincipalInfo,
    fix: LiveFix,
    audited: set[str],
) -> None:
    """Send one fix to the socket and record a who-saw-whom row the FIRST time each
    subject is seen on this connection (not per fix — that would be one row per
    position update). An audit-write failure is logged, never dropping the stream."""
    await send(live_out(fix))
    if fix.subject_id in audited:
        return
    audited.add(fix.subject_id)
    try:
        await repo.record_view(
            ctx,
            viewer_principal_id=principal.id,
            viewer_subject_id=principal.subject_id,
            target_subject_id=fix.subject_id,
            path="live",
        )
    except Exception as exc:  # noqa: BLE001 - audit must not drop the stream
        log.warning("locations.live_audit_failed", error=repr(exc))


@router.websocket("/locations/live")
async def live_feed(websocket: WebSocket) -> None:
    """Owner live feed: every device's fixes as they arrive. Closes 4401 if the owner
    cookie is absent/invalid. Races the broadcast against the socket so a disconnect
    ends the loop promptly (rather than blocking on the next fix)."""
    principal = await authenticated_owner(websocket)
    if principal is None:
        await websocket.close(code=4401)
        return
    broadcaster = cast(LiveBroadcaster, websocket.app.state.live_broadcaster)
    repo = cast(AuditSink, websocket.app.state.location_repo)
    ctx = SessionContext(principal_id=principal.id, principal_kind=principal.kind)
    queue = broadcaster.subscribe()
    await websocket.accept()
    audited: set[str] = set()
    disconnected = asyncio.ensure_future(websocket.receive())
    try:  # pragma: no cover - the WS pump is exercised at deploy, not in CI
        while True:
            nxt = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({nxt, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                nxt.cancel()
                break
            await deliver_fix(websocket.send_json, repo, ctx, principal, nxt.result(), audited)
    except WebSocketDisconnect:
        pass
    finally:
        disconnected.cancel()
        broadcaster.unsubscribe(queue)
