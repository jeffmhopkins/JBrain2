"""Reverse-proxy a request to a session's loopback dev server (Wave P2 of
``docs/JCODE_PREVIEW_HOST_PLAN.md``).

The api fronts ``<slug>-preview.<host>`` and forwards here by slug; this resolves the
slug to the session's reserved dev port and proxies the request, rewriting the Host
header to ``localhost`` so a host-pinning dev server (Vite 6+, webpack-dev-server)
accepts it — the same lesson as the cloudflared ``--http-host-header`` fix. HTTP only;
the HMR WebSocket is Wave P3. The body is buffered rather than streamed — a dev page's
assets are modest and it keeps the proxy simple and robust; SSE/large downloads are not
a preview concern.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
from starlette.requests import Request
from starlette.responses import Response

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
    """Forward ``request`` to ``http://127.0.0.1:<port>/<path>`` and return the reply.
    A 502 stands in when the dev server isn't reachable (not started, slow, or speaking
    malformed HTTP). ``client_factory`` is injectable so tests can drive a mock
    transport instead of a real socket."""
    url = httpx.URL(f"http://127.0.0.1:{port}/{path}", query=request.url.query.encode())
    body = await request.body()
    _log.debug("preview proxy %s → :%d /%s", request.method, port, path)
    async with client_factory() as client:
        try:
            resp = await client.request(
                request.method,
                url,
                headers=_request_headers(request, port),
                content=body,
            )
        except httpx.TransportError as exc:
            # Refused / timed out / malformed — the dev server isn't (yet) serving here.
            _log.warning("preview: dev server unreachable on :%d (%s)", port, exc)
            return Response("The dev server isn't reachable on this port.", 502)
    _log.debug("preview proxy ← :%d %d", port, resp.status_code)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=_response_headers(resp),
    )
