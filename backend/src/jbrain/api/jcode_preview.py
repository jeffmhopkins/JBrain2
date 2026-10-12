"""Public preview proxy: ``<slug>-preview.<host>`` → the jcode control server's
``/preview/{slug}`` (Wave P3 of ``docs/archive/JCODE_PREVIEW_HOST_PLAN.md``).

Caddy host-routes the per-session preview hostname to ``/__jcode_preview/{slug}{uri}``
on the api (one static edge rule); this forwards to the internal control server, which
resolves the slug to the session's loopback dev port (Wave P2) and proxies the dev
server. The **unguessable slug is the auth** — no owner cookie (a preview is reachable
by anyone holding the URL, like the retired trycloudflare link), and Caddy serves this
prefix ONLY on the preview subdomain (the main site 404s it) so a sandbox-run dev app
never executes on the owner origin. The owner's cookie + Authorization are stripped
here so they can never reach sandbox code; a preview-only bearer derived from the
api↔jcode token is added for that hop (the control server's management routes refuse it)
and dropped again by the control server before the dev server sees it. The path is
refused unless it provably stays under the session's ``/preview/{slug}`` prefix.

The HTTP body is buffered, not streamed (a dev page's assets are modest); the HMR
live-reload WebSocket is bridged through the same slug + origin gate (Wave P3b).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import re
from typing import cast
from urllib.parse import unquote

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


# Matches the control server's derivation (jcode_ctl.app.preview_token). No new `.env` key:
# both sides already hold JCODE_TOKEN (CLAUDE.md #10).
_PREVIEW_TOKEN_CONTEXT = b"jcode-preview"
# The ASGI server already decoded the path once; a `%2f`/`%5c` still present was
# double-encoded, and a dev server may decode it into a separator we never saw.
_ENCODED_SEPARATOR = re.compile(r"%(2f|5c)", re.IGNORECASE)
_UNSAFE_CHAR = re.compile(r"[\x00-\x1f\x7f?#]")
# Enough rounds for any plausible double-encoding; a path still changing after this is
# refused rather than trusted.
_MAX_DECODE_ROUNDS = 4


def preview_token(jcode_token: str) -> str:
    """The bearer the control server's preview routes accept — and its management API
    refuses. Derived, not the control token itself, so a request that somehow escaped the
    ``/preview/{slug}`` prefix carries nothing that opens ``/sessions``."""
    return hmac.new(jcode_token.encode(), _PREVIEW_TOKEN_CONTEXT, hashlib.sha256).hexdigest()


def _path_is_contained(path: str) -> bool:
    """False for any path that could step out of the session's preview prefix: a dot
    segment (also as `..;`, which some servers treat as `..`), a backslash, a control
    character, a `?`/`#` (decoded, it would split the upstream URL so the server sees a
    different path from the one checked here), or an encoded separator — checked at every decoding depth, since httpx and the servers
    behind it resolve what this layer forwards. Dotted names (`main.js`, `.vite`) pass."""
    for _ in range(_MAX_DECODE_ROUNDS):
        if "\\" in path or _ENCODED_SEPARATOR.search(path) or _UNSAFE_CHAR.search(path):
            return False
        if any(seg.split(";", 1)[0] in (".", "..") for seg in path.split("/")):
            return False
        decoded = unquote(path)
        if decoded == path:
            return True
        path = decoded
    return False


def _preview_host(slug: str, base_host: str) -> str:
    return f"{slug}-preview.{base_host}"


def _request_headers(request: Request, token: str) -> list[tuple[bytes, bytes]]:
    out = [
        (k, v) for k, v in request.headers.raw if k.decode("latin-1").lower() not in _DROP_REQUEST
    ]
    # The bearer authenticates THIS hop (api → control server's preview routes only); the
    # control server validates the slug and drops this header before the dev server.
    out.append((b"authorization", f"Bearer {preview_token(token)}".encode()))
    return out


async def _read_capped(request: Request) -> bytes | None:
    """The body, or None once it passes _MAX_BODY — counted as it arrives, because a
    chunked upload carries no Content-Length to check up front."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


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
    if not _path_is_contained(path):
        return Response("preview not available", status_code=404)
    base = settings.jcode_url.rstrip("/")
    target = httpx.URL(f"{base}/preview/{slug}/{path}", query=request.url.query.encode())
    # The real guard: httpx normalises dot segments when it builds the URL, so whatever
    # the check above missed shows up here as a path that left the session's prefix.
    prefix = f"{httpx.URL(base).path.rstrip('/')}/preview/{slug}"
    if target.path != prefix and not target.path.startswith(f"{prefix}/"):
        return Response("preview not available", status_code=404)
    body = await _read_capped(request)
    if body is None:
        return Response("request too large", status_code=413)
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
    headers = {"Authorization": f"Bearer {preview_token(settings.jcode_token)}"}
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
    if host != _preview_host(slug, base_host) or not _path_is_contained(path):
        await websocket.close(code=4404)
        return
    await _proxy_ws(websocket, settings, slug, path)
