"""Public preview proxy: ``<slug>-preview.<host>`` → the jcode control server's
``/preview/{slug}`` (Wave P3 of ``docs/JCODE_PREVIEW_HOST_PLAN.md``).

Caddy host-routes the per-session preview hostname to ``/__jcode_preview/{slug}{uri}``
on the api (one static edge rule); this forwards to the internal control server, which
resolves the slug to the session's loopback dev port (Wave P2) and proxies the dev
server. The **unguessable slug is the auth** — no owner cookie (a preview is reachable
by anyone holding the URL, like the retired trycloudflare link), and Caddy serves this
prefix ONLY on the preview subdomain (the main site 404s it) so a sandbox-run dev app
never executes on the owner origin. The owner's cookie + Authorization are stripped
here so they can never reach sandbox code; the api↔jcode bearer is added for that hop
and dropped again by the control server before the dev server sees it.

The HTTP body is buffered, not streamed (a dev page's assets are modest); the HMR
live-reload WebSocket is bridged through the same slug + origin gate (Wave P3b).
"""

from __future__ import annotations

import contextlib
import re
from typing import cast

import httpx
import structlog
import websockets
from fastapi import APIRouter, Request, Response, WebSocket

from jbrain.api import jcode_terminal
from jbrain.config import Settings

router = APIRouter()
log = structlog.get_logger()

# The control server mints slugs as secrets.token_hex(8) → 16 lowercase hex chars.
# Validate before forwarding so a malformed slug never reaches the control server.
_SLUG_RE = re.compile(r"^[a-f0-9]{16}$")

# Strip hop-by-hop headers (RFC 7230 §6.1), the Host (the control server rewrites it to
# localhost), and the owner's credentials — a preview is sandbox-run code that must
# never see the JBrain session cookie or any Authorization the browser sent.
_DROP_REQUEST = frozenset(
    {
        "host",
        "cookie",
        "authorization",
        "connection",
        "keep-alive",
        "proxy-authorization",
        "te",
        "upgrade",
    }
)
# httpx returns the decoded body, so framing/encoding headers no longer describe it.
# Drop Set-Cookie too: a sandbox-run dev app has no business setting a cookie the owner's
# browser would store for the preview origin.
_DROP_RESPONSE = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-length",
        "content-encoding",
        "set-cookie",
    }
)
_TIMEOUT = httpx.Timeout(35.0, connect=5.0)
# This route is unauthenticated (the slug is the secret), so cap the request body the
# api will buffer before the control server can 404 a bad slug — a dev upload past this
# is not a preview concern.
_MAX_BODY = 8 * 1024 * 1024


def _preview_host(slug: str, base_host: str) -> str:
    return f"{slug}-preview.{base_host}"


def _request_headers(request: Request, token: str) -> list[tuple[bytes, bytes]]:
    out = [
        (k, v) for k, v in request.headers.raw if k.decode("latin-1").lower() not in _DROP_REQUEST
    ]
    # The bearer authenticates THIS hop (api → control server); the control server
    # validates the slug and drops this header before the dev server is reached.
    out.append((b"authorization", f"Bearer {token}".encode()))
    return out


def _response_headers(resp: httpx.Response) -> dict[str, str]:
    return {
        k.decode("latin-1"): v.decode("latin-1")
        for k, v in resp.headers.raw
        if k.decode("latin-1").lower() not in _DROP_RESPONSE
    }


@router.api_route(
    "/__jcode_preview/{slug}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
@router.api_route(
    "/__jcode_preview/{slug}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def preview(request: Request, slug: str, path: str = "") -> Response:
    settings = cast(Settings, request.app.state.settings)
    base_host = settings.jcode_preview_base_host
    if not settings.jcode_url or not base_host or not _SLUG_RE.match(slug):
        return Response("preview not available", status_code=404)
    # Defense in depth, NOT trusting the edge: the preview must be served only on its own
    # `<slug>-preview.<base>` origin (never the main host, where a sandbox dev app could
    # read the session cookie). This also pins the path's slug to the host's slug.
    host = request.headers.get("host", "").split(":")[0].lower()
    if host != _preview_host(slug, base_host):
        return Response("preview not available", status_code=404)
    if int(request.headers.get("content-length") or 0) > _MAX_BODY:
        return Response("request too large", status_code=413)
    target = httpx.URL(
        f"{settings.jcode_url.rstrip('/')}/preview/{slug}/{path}",
        query=request.url.query.encode(),
    )
    body = await request.body()
    # An injectable transport lets tests drive the control server with no network.
    transport = getattr(request.app.state, "jcode_preview_transport", None)
    async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as client:
        try:
            resp = await client.request(
                request.method,
                target,
                headers=_request_headers(request, settings.jcode_token),
                content=body,
            )
        except httpx.HTTPError as exc:
            log.warning("jcode.preview_proxy_failed", slug=slug, error=repr(exc))
            return Response("preview is not reachable", status_code=502)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=_response_headers(resp),
    )


def _ws_url(base_url: str, slug: str, path: str, query: str) -> str:
    scheme = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    url = f"{scheme.rstrip('/')}/preview/{slug}/{path}"
    return f"{url}?{query}" if query else url


async def _proxy_ws(  # pragma: no cover - opens a live upstream socket, deploy-only
    browser: WebSocket, settings: Settings, slug: str, path: str
) -> None:
    """Bridge the browser's HMR WebSocket to the control server's preview WS, carrying the
    api↔jcode bearer and echoing the negotiated subprotocol (Vite speaks ``vite-hmr``).
    Connect upstream FIRST so the browser handshake can mirror its subprotocol."""
    requested = browser.scope.get("subprotocols") or []
    headers = {"Authorization": f"Bearer {settings.jcode_token}"}
    try:
        upstream = await websockets.connect(
            _ws_url(settings.jcode_url, slug, path, browser.url.query),
            additional_headers=headers,
            subprotocols=requested or None,
            open_timeout=10,
            max_size=None,
        )
    except Exception as exc:  # noqa: BLE001 - any upstream failure just ends the channel
        log.warning("jcode.preview_ws_failed", slug=slug, error=repr(exc))
        await browser.close(code=1011)
        return
    try:
        await browser.accept(subprotocol=upstream.subprotocol)
        await jcode_terminal._bridge(browser, upstream)
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await browser.close()


@router.websocket("/__jcode_preview/{slug}/{path:path}")
@router.websocket("/__jcode_preview/{slug}")
async def preview_ws(websocket: WebSocket, slug: str, path: str = "") -> None:
    """The HMR live-reload channel. Same slug + origin gate as the HTTP route — the
    unguessable slug is the auth, and the request Host must be the preview subdomain so a
    sandbox dev app's WS can't be hijacked through the owner origin. Closes 4404 before
    any upstream connect on a malformed slug / wrong Host / unconfigured jcode."""
    # No Origin/CSWSH check here (unlike the terminal WS): that defense exists because the
    # terminal rides the owner's ambient session cookie. This route carries NO ambient
    # credential — it reads/forwards no cookie, and the capability is the unguessable slug.
    # A cross-origin page that doesn't know the slug reaches nothing; one that does already
    # has the full capability. The Host gate below is server-side origin defense in depth.
    settings = cast(Settings, websocket.app.state.settings)
    base_host = settings.jcode_preview_base_host
    if not settings.jcode_url or not base_host or not _SLUG_RE.match(slug):
        await websocket.close(code=4404)
        return
    host = websocket.headers.get("host", "").split(":")[0].lower()
    if host != _preview_host(slug, base_host):
        await websocket.close(code=4404)
        return
    await _proxy_ws(websocket, settings, slug, path)
