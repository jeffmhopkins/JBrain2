"""Reverse-proxy a request to a session's loopback dev server (Wave P2 of
``docs/JCODE_PREVIEW_HOST_PLAN.md``).

The api fronts ``<slug>-preview.<host>`` and forwards here by slug; this resolves the
slug to the session's reserved dev port and proxies the request, rewriting the Host
header to ``localhost`` so a host-pinning dev server (Vite 6+, webpack-dev-server)
accepts it — the cloudflared ``--http-host-header`` lesson. ``proxy_http`` buffers the
body rather than streaming (a dev page's assets are modest); ``proxy_ws`` bridges the
HMR live-reload WebSocket to the dev server (Wave P3b).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

import httpx
import websockets
from starlette.requests import Request
from starlette.responses import Response
from starlette.websockets import WebSocket

_log = logging.getLogger("jcode_ctl.preview")

# Don't forward hop-by-hop headers (RFC 7230 §6.1), the Host (rewritten below), or
# Authorization — that header carries the api↔jcode bearer on this hop, and the dev
# server is sandbox-run code that must never see the control-server token.
_DROP_REQUEST = frozenset(
    {
        "host",
        "authorization",
        "connection",
        "keep-alive",
        "proxy-authorization",
        "te",
        "upgrade",
        # Don't let a caller forge the client identity the sandbox dev server sees.
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
    }
)
# A dev server can be slow to answer the first request after it starts (Vite compiles
# on demand), so allow a generous read window; the connect timeout stays short so a
# port with nothing listening fails fast to a 502.
_TIMEOUT = httpx.Timeout(30.0, connect=2.0)

# Reach the dev server on whichever loopback family it bound, IPv4 first then IPv6. Vite
# (and other servers) default their host to `localhost`, which Node frequently binds to
# `::1` ONLY — so a plain `npm run dev` (no `--host`) listens on [::1]:<port> and is
# unreachable on 127.0.0.1. Trying both makes the common dev server work with no flags;
# a refused connect on loopback returns instantly, so the fallback costs nothing.
_DEV_HOSTS = ("127.0.0.1", "[::1]")


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT)


# httpx returns the DECODED body, so the upstream's framing/encoding headers no longer
# describe it — drop them and let the Response recompute Content-Length from the bytes.
_DROP_RESPONSE = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-length",
        "content-encoding",
    }
)


def _request_headers(request: Request, port: int) -> list[tuple[bytes, bytes]]:
    out = [
        (k, v)
        for k, v in request.headers.raw
        if k.decode("latin-1").lower() not in _DROP_REQUEST
    ]
    # The dev server trusts localhost; the public preview host would trip its allowlist.
    out.append((b"host", f"localhost:{port}".encode()))
    return out


def _response_headers(resp: httpx.Response) -> dict[str, str]:
    return {
        k.decode("latin-1"): v.decode("latin-1")
        for k, v in resp.headers.raw
        if k.decode("latin-1").lower() not in _DROP_RESPONSE
    }


async def proxy_http(
    port: int,
    path: str,
    request: Request,
    *,
    client_factory: Callable[[], httpx.AsyncClient] = _default_client,
) -> Response:
    """Forward ``request`` to the session's loopback dev server and return the reply,
    trying both loopback families (IPv4 then IPv6) so a ``localhost``-bound server like
    Vite is reached without ``--host``. A 502 stands in when nothing is reachable on
    EITHER family, or the server errors once connected (slow / malformed).
    ``client_factory`` is injectable so tests can drive a mock transport."""
    body = await request.body()
    headers = _request_headers(request, port)
    query = request.url.query.encode()
    _log.debug("preview proxy %s → :%d /%s", request.method, port, path)
    async with client_factory() as client:
        refused: httpx.ConnectError | None = None
        for host in _DEV_HOSTS:
            url = httpx.URL(f"http://{host}:{port}/{path}", query=query)
            try:
                resp = await client.request(
                    request.method, url, headers=headers, content=body
                )
            except httpx.ConnectError as exc:
                # Nothing on this family — try the other loopback before giving up.
                refused = exc
                continue
            except httpx.TransportError as exc:
                # Connected but failed mid-exchange (timeout / malformed): the server IS
                # here, so don't retry the other family — report it.
                _log.warning("preview: dev server errored on :%d (%s)", port, exc)
                return Response("The dev server isn't reachable on this port.", 502)
            _log.debug("preview proxy ← :%d %d", port, resp.status_code)
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=_response_headers(resp),
            )
    # Refused on every loopback family — nothing is serving on this port.
    _log.warning("preview: dev server unreachable on :%d (%s)", port, refused)
    return Response("The dev server isn't reachable on this port.", 502)


async def _pump_browser_to_dev(
    browser: WebSocket, dev: Any
) -> None:  # pragma: no cover - live pump, deploy-only
    """Browser → dev server until the browser disconnects, preserving binary vs text."""
    while True:
        message = await browser.receive()
        if message.get("type") == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None:
            await dev.send(data)
            continue
        text = message.get("text")
        if text is not None:
            await dev.send(text)


async def _pump_dev_to_browser(
    dev: Any, browser: WebSocket
) -> None:  # pragma: no cover - live pump, deploy-only
    """Dev server → browser, preserving binary vs text framing (HMR sends text JSON)."""
    async for message in dev:
        if isinstance(message, bytes):
            await browser.send_bytes(message)
        else:
            await browser.send_text(message)


async def _bridge_ws(
    browser: WebSocket, dev: Any
) -> None:  # pragma: no cover - live pump, deploy-only
    pumps = {
        asyncio.ensure_future(_pump_browser_to_dev(browser, dev)),
        asyncio.ensure_future(_pump_dev_to_browser(dev, browser)),
    }
    done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()  # surface a real error (not a normal disconnect)


async def proxy_ws(  # pragma: no cover - opens a live upstream socket, deploy-only
    browser: WebSocket,
    port: int,
    path: str,
    query: str,
) -> None:
    """Bridge an authenticated browser WebSocket to the dev server's HMR live-reload WS,
    trying both loopback families (IPv4 then IPv6) like the HTTP proxy so a
    ``localhost``/IPv6-bound dev server is reached without ``--host``. Connect upstream
    FIRST so the browser handshake can echo the negotiated subprotocol (Vite speaks
    ``vite-hmr``); if no dev WS answers on either family, close cleanly, don't hang."""
    # Connect by loopback IP literal (127.0.0.1 / [::1]). Unlike the HTTP proxy (which
    # forwards the incoming preview Host, so must rewrite it to localhost), websockets
    # builds a fresh handshake with that IP literal as Host — which a host-pinning dev
    # server allows — so the preview host never reaches the dev WS; no rewrite needed.
    suffix = f"?{query}" if query else ""
    requested = browser.scope.get("subprotocols") or []
    _log.debug("preview ws → :%d /%s (subprotocols=%s)", port, path, requested)
    dev = None
    last_exc: Exception | None = None
    for host in _DEV_HOSTS:
        try:
            dev = await websockets.connect(
                f"ws://{host}:{port}/{path}{suffix}",
                subprotocols=requested or None,
                open_timeout=5,
                max_size=None,
            )
            break
        except Exception as exc:
            last_exc = exc
    if dev is None:
        _log.warning("preview ws: dev WS unreachable on :%d (%s)", port, last_exc)
        await browser.close(code=1011)
        return
    try:
        await browser.accept(subprotocol=dev.subprotocol)
        await _bridge_ws(browser, dev)
    finally:
        with contextlib.suppress(Exception):
            await dev.close()
        with contextlib.suppress(Exception):
            await browser.close()
