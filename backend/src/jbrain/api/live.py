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

The handshake is guarded by a strict **Origin allow-list** (CSWSH defense, B8): a
browser always sends `Origin`, so a cross-site page using a victim's cookie is
rejected before auth.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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
    """CSWSH gate: with an allow-list configured, a *present* `Origin` must match;
    an absent Origin (a native, non-browser client) is allowed, and an empty
    allow-list disables the check (dev). A browser always sends Origin, so a
    cross-site page can never ride a victim's cookie onto the feed."""
    allowed = settings.allowed_ws_origins
    if not allowed:
        return True
    origin = websocket.headers.get("origin")
    return origin is None or origin in allowed


async def authenticated_viewer(websocket: WebSocket) -> PrincipalInfo | None:
    """The principal from the session cookie on the WS handshake — the owner or a
    member (device-key) — or None for an absent/invalid cookie or any other kind."""
    repo = cast(AuthRepo, websocket.app.state.auth_repo)
    settings = cast(Settings, websocket.app.state.settings)
    token = websocket.cookies.get(settings.session_cookie, "")
    principal = await service.authenticate(repo, token)
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
    decision is cached for the connection's life, so the fan-out costs one lookup
    per new subject, not one per fix."""
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


@router.websocket("/locations/live")
async def live_feed(websocket: WebSocket) -> None:
    """Live feed for the owner (every device) or a member (its own + family group).
    Closes 4403 on a disallowed Origin, 4401 on an absent/invalid cookie. Races the
    broadcast against the socket so a disconnect ends the loop promptly."""
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
    ctx = viewer_context(principal)
    queue = broadcaster.subscribe()
    await websocket.accept()
    audited: set[str] = set()
    visible: dict[str, bool] = {}
    disconnected = asyncio.ensure_future(websocket.receive())
    try:  # pragma: no cover - the WS pump is exercised at deploy, not in CI
        while True:
            nxt = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({nxt, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                nxt.cancel()
                break
            await deliver_fix(
                websocket.send_json, repo, viewscope, ctx, principal, nxt.result(), audited, visible
            )
    except WebSocketDisconnect:
        pass
    finally:
        disconnected.cancel()
        broadcaster.unsubscribe(queue)
