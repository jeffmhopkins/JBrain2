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

HTTP only; the HMR WebSocket is Wave P3b. The body is buffered, not streamed (a dev
page's assets are modest), matching the control-server proxy.
"""

from __future__ import annotations

import re
from typing import cast

import httpx
import structlog
from fastapi import APIRouter, Request, Response

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
