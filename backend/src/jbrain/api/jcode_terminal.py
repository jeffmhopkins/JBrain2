"""Interactive terminal WebSocket proxy: the owner's browser ↔ the jcode sandbox shell.

A separate router from the REST jcode surface because WebSocket auth reads the
session cookie off the handshake (not a `Request`/`OwnerDep` dependency), exactly
like the live-location feed. The browser's xterm.js connects here; we authenticate
the owner, then bridge raw bytes to the control server's PTY route
(`ws://jcode:9100/sessions/{sid}/terminal`) over an internal, token-authed socket.

Owner-only and Origin-gated (CSWSH defense): code mode is the owner's alone, and a
live terminal into the sandbox is the same trust boundary the headless agent already
runs at (isolated network, throwaway checkout, bypassPermissions).
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog
import websockets
from fastapi import APIRouter, WebSocket

from jbrain.api.client_ip import device_key
from jbrain.api.live import origin_allowed
from jbrain.api.session_cookie import session_token
from jbrain.api.ws_reauth import SocketAuth, run_until_revoked
from jbrain.auth import service
from jbrain.config import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterable

    from jbrain.auth.service import AuthRepo, PrincipalInfo


class _BrowserWS(Protocol):
    """The slice of the browser-side WebSocket the pumps use — satisfied by Starlette's
    WebSocket and by the test fakes (so neither side needs the concrete type)."""

    async def receive(self) -> Any: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def send_text(self, data: str) -> None: ...


router = APIRouter()
log = structlog.get_logger()

# Session ids are the control server's hex tokens; mirror the REST surface's gate so a
# malformed id can never be interpolated into the upstream URL.
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def _cookie_principal(websocket: WebSocket) -> PrincipalInfo | None:
    """The principal from the session cookie on the WS handshake (owner OR a redeemed
    share link), or None. Unlike the location feed's `authenticated_viewer` this keeps
    ALL kinds — the per-session scope check (`_may_access`) decides, not a kind allow-list."""
    repo = cast("AuthRepo", websocket.app.state.auth_repo)
    settings = cast(Settings, websocket.app.state.settings)
    return await service.authenticate(
        repo, session_token(websocket, settings), device=device_key(websocket)
    )


def _may_access(principal: PrincipalInfo, sid: str) -> bool:
    """The owner reaches any session's shell; a share link only the one it's scoped to."""
    return principal.kind == "owner" or (
        principal.kind == "jcode_share_link" and principal.jcode_session_id == sid
    )


def ws_url(base_url: str, sid: str) -> str:
    """The control server's terminal WS URL for a session, from its http base url."""
    scheme = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return f"{scheme.rstrip('/')}/sessions/{sid}/terminal"


async def browser_to_upstream(browser: _BrowserWS, upstream: Any) -> None:
    """Pump browser frames to the shell: binary keystrokes/paste verbatim, text frames
    (the resize control) as-is. Returns when the browser disconnects."""
    while True:
        message = await browser.receive()
        if message.get("type") == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None:
            await upstream.send(data)
            continue
        text = message.get("text")
        if text is not None:
            await upstream.send(text)


async def upstream_to_browser(upstream: AsyncIterable[Any], browser: _BrowserWS) -> None:
    """Pump shell output to the browser, preserving binary vs. text framing."""
    async for message in upstream:
        if isinstance(message, bytes):
            await browser.send_bytes(message)
        else:
            await browser.send_text(message)


async def _bridge(browser: WebSocket, upstream: Any) -> None:
    import asyncio

    pumps = {
        asyncio.ensure_future(browser_to_upstream(browser, upstream)),
        asyncio.ensure_future(upstream_to_browser(upstream, browser)),
    }
    try:
        done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Also when the bridge itself is cancelled (a revoked session): no pump may
        # outlive it, still reading the browser or writing to a closed upstream.
        for task in pumps:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*pumps, return_exceptions=True)
    for task in done:
        task.result()  # surface a real error (not a normal disconnect) to the caller


async def _proxy(browser: WebSocket, settings: Settings, sid: str) -> None:
    headers = {"Authorization": f"Bearer {settings.jcode_token}"}
    try:
        async with websockets.connect(
            ws_url(settings.jcode_url, sid),
            additional_headers=headers,
            max_size=None,
            open_timeout=10,
        ) as upstream:
            await _bridge(browser, upstream)
    except Exception as exc:  # noqa: BLE001 - any upstream failure just ends the tab
        log.warning("jcode.terminal_proxy_failed", sid=sid, error=repr(exc))
    with contextlib.suppress(Exception):
        await browser.close()


@router.websocket("/jcode/sessions/{sid}/terminal")
async def jcode_terminal(websocket: WebSocket, sid: str) -> None:
    """Bridge the owner's (or the session's share recipient's) xterm to the sandbox shell.
    Closes 4403 on a disallowed Origin, 4401 for a non-owner / absent cookie, 4404 when
    code mode is off or the id is malformed — all before the upstream socket is opened —
    and 4401 once the session is revoked or lapses (a revoked share's terminal ends; the
    shell itself lives on, since the owner types into the same one)."""
    settings = cast(Settings, websocket.app.state.settings)
    if not origin_allowed(websocket, settings):
        await websocket.close(code=4403)
        return
    principal = await _cookie_principal(websocket)
    if principal is None or not _may_access(principal, sid):
        await websocket.close(code=4401)
        return
    if not settings.jcode_url or not _SID_RE.match(sid):
        await websocket.close(code=4404)
        return
    await websocket.accept()
    await run_until_revoked(
        websocket, SocketAuth(websocket, principal), _proxy(websocket, settings, sid)
    )
