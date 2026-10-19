"""Every owner-site API request must come from an enrolled phone that the owner confirmed and
has not revoked (docs/plans/OWNER_TUNNEL_PLAN.md §3.4, §3.5).

Caddy accepts any certificate the enrollment CA signed, so a revoked phone still completes the
handshake; this guard is what refuses it, on every `/api` route and WebSocket, authenticated
or not. Static assets are Caddy's and carry nothing. Pure ASGI because `@app.middleware`
never sees WebSocket handshakes. The phone found is left in the scope state for the route
that mints its session.

The check is held, not just made at the start: a revoked phone's open notification or log
stream, or socket, would otherwise keep flowing until the phone hung up. So every owner-site
request is re-checked on the WebSocket re-auth tick and at once when its phone is revoked
(`kick_device`), and ended when the phone no longer passes.

The owner site signs in only through the phone's own mint (`/api/owner-devices/session`). Any
other route that would set a session cookie here (the owner-key login, a redeem) is refused
at the response, whatever route it is: such a cookie is unbound, so it could never ride the
tunnel, and refusing it at this one sink covers routes added later too.
"""

import asyncio
import contextlib
from typing import Any, cast

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jbrain.api import client_ip
from jbrain.api.session_cookie import session_cookie_names
from jbrain.api.ws_reauth import MAX_CHECK_FAILURES, REAUTH_INTERVAL_S
from jbrain.owner_tunnel.devices import OwnerDeviceInfo, OwnerDeviceRepo

OWNER_INGRESS = "owner-tunnel"
STATE_KEY = "owner_device"
_REFUSED = b'{"detail":"device not enrolled"}'
_PENDING = b'{"detail":"waiting for the owner to confirm this phone"}'
_NOT_HERE = b'{"detail":"sign in with the phone, not here"}'
MINT_PATH = "/api/owner-devices/session"
_WATCHERS_ATTR = "owner_device_watchers"
# Shared with ws_reauth so tests shorten both with one setting.
_INTERVAL_ATTR = "ws_reauth_interval_s"


def _watchers(app: Any) -> dict[str, set[asyncio.Event]]:
    watchers = getattr(app.state, _WATCHERS_ATTR, None)
    if watchers is None:
        watchers = {}
        setattr(app.state, _WATCHERS_ATTR, watchers)
    return cast("dict[str, set[asyncio.Event]]", watchers)


def kick_device(app: Any, device_id: str) -> None:
    """Re-check now every request this process is serving for the phone `device_id`."""
    for woken in list(_watchers(app).get(device_id, ())):
        woken.set()


def _passes(device: OwnerDeviceInfo | None) -> bool:
    return device is not None and device.confirmed


async def _refuse(scope: Scope, send: Send, body: bytes = _REFUSED) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 4403, "reason": ""})
        return
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _sets_session(message: Message, app: Any) -> bool:
    """Whether a response start sets (not clears) a session cookie."""
    for key, value in message.get("headers", ()):
        if key.lower() != b"set-cookie":
            continue
        names = tuple(n.encode() for n in session_cookie_names(app.state.settings))
        name, _, rest = value.partition(b"=")
        token = rest.split(b";", 1)[0].strip(b' "')
        if name.strip() in names and token:
            return True
    return False


class OwnerDeviceGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not scope["path"].startswith("/api"):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        if client_ip.ingress(conn) != OWNER_INGRESS:
            await self.app(scope, receive, send)
            return
        key = client_ip.device_key(conn)
        repo = cast(OwnerDeviceRepo, scope["app"].state.owner_device_repo)
        device = await repo.touch_active(key, OWNER_INGRESS) if key else None
        if key is None or device is None:
            await _refuse(scope, send)
            return
        if not device.confirmed:
            await _refuse(scope, send, _PENDING)
            return
        scope.setdefault("state", {})[STATE_KEY] = device
        await self._while_enrolled(scope, receive, send, repo, device, key)

    async def _while_enrolled(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        repo: OwnerDeviceRepo,
        device: OwnerDeviceInfo,
        key: str,
    ) -> None:
        app = scope["app"]
        interval = float(getattr(app.state, _INTERVAL_ATTR, REAUTH_INTERVAL_S))
        started = False
        closed = False
        swallowed = False
        mints = scope["path"] == MINT_PATH

        async def tracked(message: Message) -> None:
            nonlocal started, closed, swallowed
            if swallowed:
                return
            if (
                message["type"] == "http.response.start"
                and not mints
                and _sets_session(message, app)
            ):
                swallowed = True
                started = True
                await _refuse(scope, send, _NOT_HERE)
                return
            started = started or message["type"] in ("http.response.start", "websocket.accept")
            closed = closed or message["type"] == "websocket.close"
            await send(message)

        woken = asyncio.Event()
        peers = _watchers(app).setdefault(device.id, set())
        peers.add(woken)
        task = asyncio.ensure_future(self.app(scope, receive, tracked))
        failures = 0
        try:
            while True:
                wake = asyncio.ensure_future(woken.wait())
                await asyncio.wait({task, wake}, timeout=interval, return_when="FIRST_COMPLETED")
                wake.cancel()
                if task.done():
                    break
                woken.clear()
                try:
                    enrolled = _passes(await repo.touch_active(key, OWNER_INGRESS))
                    failures = 0
                except Exception:
                    # A DB blip must not cut the owner off; a check that keeps failing must.
                    failures += 1
                    enrolled = failures < MAX_CHECK_FAILURES
                if enrolled:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                if not started:
                    await _refuse(scope, send)
                elif scope["type"] == "websocket" and not closed:
                    await send({"type": "websocket.close", "code": 4403, "reason": ""})
                # An HTTP response already under way is cut short: the server closes the
                # connection when the app returns without finishing it.
                return
            await task
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            peers.discard(woken)
            if not peers:
                _watchers(app).pop(device.id, None)
