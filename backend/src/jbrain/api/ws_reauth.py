"""Re-authentication for long-lived WebSockets (security plan S3.1, F10).

A WS authenticates once, on its handshake; without this a socket opened before its
session or principal was revoked (a member removed, a jcode share revoked, a share link
lapsing) would keep streaming for as long as the browser held it open. `SocketAuth`
re-resolves the handshake's own session token on a ~30 s tick and resolves `revoked()`
the first time it no longer names the same principal, so the caller closes 4401.

The tick is a timed wait, not a wait on socket traffic, so an idle socket is re-checked
as often as a busy one. A revoke route in this process can also `kick` a principal's
sockets so they close at once instead of on the next tick; with more than one api
worker, the tick is the backstop for sockets held by another one.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, cast

from jbrain.api.session_cookie import session_token
from jbrain.auth import service

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from starlette.websockets import WebSocket

    from jbrain.auth.service import AuthRepo, PrincipalInfo
    from jbrain.config import Settings

REAUTH_INTERVAL_S = 30.0
REVOKED_CLOSE_CODE = 4401

_REGISTRY_ATTR = "ws_auth_registry"
# Tests shorten the tick by setting this on app.state.
_INTERVAL_ATTR = "ws_reauth_interval_s"


def _registry(app: Any) -> dict[str, set[SocketAuth]]:
    state = app.state
    registry = getattr(state, _REGISTRY_ATTR, None)
    if registry is None:
        registry = {}
        setattr(state, _REGISTRY_ATTR, registry)
    return cast("dict[str, set[SocketAuth]]", registry)


def kick(app: Any, principal_id: str) -> None:
    """Wake every socket this process holds for `principal_id` so each re-checks now.
    Whatever it finds decides — a kick never closes a socket that still authenticates."""
    for watcher in list(_registry(app).get(principal_id, ())):
        watcher.wake()


class SocketAuth:
    """Watches one accepted socket's handshake session for revocation or expiry."""

    def __init__(self, websocket: WebSocket, principal: PrincipalInfo) -> None:
        self._app = websocket.app
        settings = cast("Settings", websocket.app.state.settings)
        # The token is read once: a WS handshake's cookies never change afterwards.
        self._token = session_token(websocket, settings)
        self.principal = principal
        self.interval = float(getattr(websocket.app.state, _INTERVAL_ATTR, REAUTH_INTERVAL_S))
        self._woken = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def wake(self) -> None:
        # Thread-safe: the revoking request need not run on the socket's event loop.
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._woken.set)

    async def still_valid(self) -> bool:
        repo = cast("AuthRepo", self._app.state.auth_repo)
        current = await service.authenticate(repo, self._token)
        return current is not None and current.id == self.principal.id

    async def revoked(self, on_tick: Callable[[], None] | None = None) -> None:
        """Return once the session no longer authenticates as this principal. `on_tick`
        runs after every check that passes, for callers that cache authorization
        decisions which can change without the session itself being revoked."""
        self._loop = asyncio.get_running_loop()
        peers = _registry(self._app).setdefault(self.principal.id, set())
        peers.add(self)
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._woken.wait(), timeout=self.interval)
                self._woken.clear()
                if not await self.still_valid():
                    return
                if on_tick is not None:
                    on_tick()
        finally:
            peers.discard(self)
            registry = _registry(self._app)
            if not peers and registry.get(self.principal.id) is peers:
                del registry[self.principal.id]


async def run_until_revoked(
    websocket: WebSocket,
    auth: SocketAuth,
    work: Coroutine[Any, Any, None],
    *,
    on_tick: Callable[[], None] | None = None,
) -> None:
    """Run a socket's pump until it ends by itself or its session stops authenticating;
    in the second case the pump is cancelled (closing anything it holds open upstream)
    and the browser is closed 4401, as a handshake without a valid session would be."""
    pump = asyncio.ensure_future(work)
    watch = asyncio.ensure_future(auth.revoked(on_tick))
    try:
        done, _ = await asyncio.wait({pump, watch}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (pump, watch):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(pump, watch, return_exceptions=True)
    if pump in done:
        pump.result()
        return
    with contextlib.suppress(Exception):
        await websocket.close(code=REVOKED_CLOSE_CODE)
