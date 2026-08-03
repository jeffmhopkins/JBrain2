"""Live terminal WebSocket proxy: the owner's browser <-> a jlaunch job's PTY.

A separate router from the REST jlaunch surface because WebSocket auth reads the session
cookie off the handshake (not a `Request`/`OwnerDep` dependency), exactly like the jcode
terminal and the live-location feed. The browser's xterm.js connects here; we authenticate
the OWNER, then bridge raw bytes to the control server's PTY route
(`ws://jlaunch:9101/jobs/{jid}/terminal`) over an internal, token-authed socket.

Owner-only and Origin-gated (CSWSH defense). Unlike a jcode session, a jlaunch job is
never shared into the live shell — a public recipient gets the read-only results page,
not the terminal — so there is no share-link principal path here.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, Any, cast

import structlog
import websockets
from fastapi import APIRouter, WebSocket

from jbrain.api.live import origin_allowed
from jbrain.auth import service
from jbrain.config import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterable

    from jbrain.auth.service import AuthRepo, PrincipalInfo


router = APIRouter()
log = structlog.get_logger()

# Job ids are the control server's hex tokens; mirror the REST gate so a malformed id can
# never be interpolated into the upstream URL.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def _cookie_principal(websocket: WebSocket) -> PrincipalInfo | None:
    repo = cast("AuthRepo", websocket.app.state.auth_repo)
    settings = cast(Settings, websocket.app.state.settings)
    token = websocket.cookies.get(settings.session_cookie, "")
    return await service.authenticate(repo, token)


def ws_url(base_url: str, jid: str) -> str:
    """The control server's terminal WS URL for a job, from its http base url."""
    scheme = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return f"{scheme.rstrip('/')}/jobs/{jid}/terminal"


async def browser_to_upstream(browser: Any, upstream: Any) -> None:
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


async def upstream_to_browser(upstream: AsyncIterable[Any], browser: Any) -> None:
    """Pump shell output to the browser, preserving binary vs. text framing."""
    async for message in upstream:
        if isinstance(message, bytes):
            await browser.send_bytes(message)
        else:
            await browser.send_text(message)


async def _bridge(browser: WebSocket, upstream: Any) -> None:  # pragma: no cover - live pump
    import asyncio

    pumps = {
        asyncio.ensure_future(browser_to_upstream(browser, upstream)),
        asyncio.ensure_future(upstream_to_browser(upstream, browser)),
    }
    done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()  # surface a real error (not a normal disconnect) to the caller


async def _proxy(browser: WebSocket, settings: Settings, jid: str) -> None:  # pragma: no cover
    headers = {"Authorization": f"Bearer {settings.jlaunch_token}"}
    try:
        async with websockets.connect(
            ws_url(settings.jlaunch_url, jid),
            additional_headers=headers,
            max_size=None,
            open_timeout=10,
        ) as upstream:
            await _bridge(browser, upstream)
    except Exception as exc:  # noqa: BLE001 - any upstream failure just ends the tab
        log.warning("jlaunch.terminal_proxy_failed", jid=jid, error=repr(exc))
    with contextlib.suppress(Exception):
        await browser.close()


@router.websocket("/jlaunch/jobs/{jid}/terminal")
async def jlaunch_terminal(websocket: WebSocket, jid: str) -> None:
    """Bridge the owner's xterm to a job's PTY. Closes 4403 on a disallowed Origin, 4401
    for a non-owner / absent cookie, 4404 when the launcher is off or the id is malformed
    — all before the upstream socket is opened."""
    settings = cast(Settings, websocket.app.state.settings)
    if not origin_allowed(websocket, settings):
        await websocket.close(code=4403)
        return
    principal = await _cookie_principal(websocket)
    if principal is None or principal.kind != "owner":
        await websocket.close(code=4401)
        return
    if not settings.jlaunch_url or not _ID_RE.match(jid):
        await websocket.close(code=4404)
        return
    await websocket.accept()
    await _proxy(websocket, settings, jid)
