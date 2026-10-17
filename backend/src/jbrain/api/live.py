"""The live location WebSocket (JBrain360 M3b owner feed, M4d member feed).

A separate router from the REST surface because WebSocket auth reads the session
cookie off the handshake, not via a `Request` dependency. The browser never holds
MQTT creds — it receives a server-filtered stream off the in-process
`LiveBroadcaster` (plan B4).

Two viewers share one endpoint:
- the **owner** (cookie kind `owner`) sees every device's fixes;
- a **member** (device-key cookie from `/session/mint`) sees only its own subject
  and its family group — the same `viewer_may_see` firewall the history path
  enforces in RLS, applied per-fix here against the live fan-out.

The handshake is guarded by an **Origin check** (CSWSH defense, B8): a browser always
sends `Origin`, so a cross-site or same-site page riding a victim's cookie is rejected
before auth — by the allow-list when one is configured, else by Origin == Host.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from jbrain.api.client_ip import device_key
from jbrain.api.same_site import origin_matches_host
from jbrain.api.session_cookie import session_token
from jbrain.api.ws_reauth import SocketAuth, run_until_revoked
from jbrain.auth import service
from jbrain.auth.service import AuthRepo, PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, device_context
from jbrain.locations.live import LiveBroadcaster, LiveFix
from jbrain.locations.viewscope import ViewScopeRepo

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


def origin_allowed(websocket: WebSocket, settings: Settings) -> bool:
    """CSWSH gate. An absent `Origin` (a native, non-browser client) passes. A present one
    must be on the allow-list when one is configured, else must name the very host the
    handshake was sent to. Fail closed by default: a same-site page (a jcode preview on a
    sibling subdomain) gets the victim's cookies attached, and only its Origin gives it
    away. The Host check ignores the scheme because the tunnel hands the api plain HTTP."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    allowed = settings.allowed_ws_origins
    if allowed:
        return origin in allowed
    return origin_matches_host(origin, websocket.headers.get("host"))


async def authenticated_viewer(websocket: WebSocket) -> PrincipalInfo | None:
    """The principal from the session cookie on the WS handshake — the owner or a
    member (device-key) — or None for an absent/invalid cookie or any other kind."""
    repo = cast(AuthRepo, websocket.app.state.auth_repo)
    settings = cast(Settings, websocket.app.state.settings)
    principal = await service.authenticate(
        repo, session_token(websocket, settings), device=device_key(websocket)
    )
    if principal is None or principal.kind not in ("owner", "device_key"):
        return None
    return principal


def viewer_context(principal: PrincipalInfo) -> SessionContext:
    """The audit-write ctx for this viewer: a full-owner session, or the member's
    `device_context` (so the `view_audit` WITH CHECK attributes the view to the
    member's own subject — a member cannot forge another's view)."""
    if principal.kind == "owner":
        return SessionContext(principal_id=principal.id, principal_kind="owner")
    return device_context(principal.id, principal.subject_id)


async def visible_to(
    principal: PrincipalInfo,
    subject_id: str,
    viewscope: ViewScopeRepo,
    cache: dict[str, bool],
) -> bool:
    """May this viewer see this subject's live fix? The owner sees all; a member
    sees its own subject and its family group (`viewer_may_see`). The per-subject
    decision is cached until the socket's next re-auth tick (which clears it), so
    the fan-out costs one lookup per subject per tick, not one per fix."""
    if principal.kind == "owner" or subject_id == principal.subject_id:
        return True
    if subject_id not in cache:
        cache[subject_id] = await viewscope.may_view(principal.subject_id, subject_id)
    return cache[subject_id]


def live_out(fix: LiveFix) -> dict[str, Any]:
    return {
        "subject_id": fix.subject_id,
        "lat": fix.latitude,
        "lon": fix.longitude,
        "accuracy_m": fix.accuracy_m,
        "battery_pct": fix.battery_pct,
        "velocity_mps": fix.velocity_mps,
        "captured_at": fix.captured_at.isoformat(),
    }


async def deliver_fix(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    repo: AuditSink,
    viewscope: ViewScopeRepo,
    ctx: SessionContext,
    principal: PrincipalInfo,
    fix: LiveFix,
    audited: set[str],
    visible: dict[str, bool],
) -> None:
    """Send one in-scope fix to the socket and record a who-saw-whom row the FIRST
    time each subject is seen on this connection (not per fix). A fix for a subject
    the viewer may not see is dropped silently — never sent, never audited. An
    audit-write failure is logged, never dropping the stream."""
    if not await visible_to(principal, fix.subject_id, viewscope, visible):
        return
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


async def _pump(
    websocket: WebSocket,
    queue: "asyncio.Queue[LiveFix]",
    deliver: Callable[[LiveFix], Awaitable[None]],
) -> None:
    """Forward broadcast fixes until the browser goes away. Races the broadcast against
    the socket so a disconnect ends the loop promptly."""
    disconnected = asyncio.ensure_future(websocket.receive())
    nxt: asyncio.Future[LiveFix] | None = None
    try:
        while True:
            nxt = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({nxt, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                break
            await deliver(nxt.result())
    except WebSocketDisconnect:
        pass
    finally:
        # Also on cancellation (a revoked session), so no orphaned waits remain.
        disconnected.cancel()
        if nxt is not None:
            nxt.cancel()


@router.websocket("/locations/live")
async def live_feed(websocket: WebSocket) -> None:
    """Live feed for the owner (every device) or a member (its own + family group).
    Closes 4403 on a disallowed Origin, 4401 on an absent/invalid cookie, and 4401 later
    once that session is revoked or lapses (`ws_reauth`)."""
    settings = cast(Settings, websocket.app.state.settings)
    if not origin_allowed(websocket, settings):
        await websocket.close(code=4403)
        return
    principal = await authenticated_viewer(websocket)
    if principal is None:
        await websocket.close(code=4401)
        return
    broadcaster = cast(LiveBroadcaster, websocket.app.state.live_broadcaster)
    repo = cast(AuditSink, websocket.app.state.location_repo)
    viewscope = cast(ViewScopeRepo, websocket.app.state.view_scope_repo)
    queue = broadcaster.subscribe()
    try:
        await websocket.accept()
        await _stream(websocket, principal, queue, repo, viewscope)
    finally:
        broadcaster.unsubscribe(queue)


async def _stream(
    websocket: WebSocket,
    principal: PrincipalInfo,
    queue: "asyncio.Queue[LiveFix]",
    repo: AuditSink,
    viewscope: ViewScopeRepo,
) -> None:
    ctx = viewer_context(principal)
    audited: set[str] = set()
    visible: dict[str, bool] = {}

    async def deliver(fix: LiveFix) -> None:
        await deliver_fix(
            websocket.send_json, repo, viewscope, ctx, principal, fix, audited, visible
        )

    # The visibility cache is dropped on every re-check: a member taken out of a family
    # group keeps a valid session, so only a fresh `viewer_may_see` stops that subject.
    await run_until_revoked(
        websocket,
        SocketAuth(websocket, principal),
        _pump(websocket, queue, deliver),
        on_tick=visible.clear,
    )
