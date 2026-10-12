"""Refuse cookie-bearing requests that a sibling subdomain's page could forge.

SameSite cookies stop cross-SITE forgery, but a jcode preview at `<slug>-preview.<host>`
runs arbitrary code on the SAME site as the app, so the browser attaches even Strict
cookies to its requests and WebSocket handshakes. The fetch-metadata and Origin headers
are what still tell such a request apart, and a page cannot forge them. This guard is
pure ASGI because `@app.middleware("http")` never sees WebSocket handshakes, which are
where the owner's shell and the family location feed live.

Requests without a session cookie, and non-browser clients that send neither header
(OkHttp, curl, firmware), pass untouched.
"""

import json
from typing import cast

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jbrain.api.session_cookie import apply_migration, session_cookie_names
from jbrain.config import Settings

_REFUSED = json.dumps({"detail": "cross-site request refused"}).encode()
_DEFAULT_PORTS = frozenset({"80", "443"})
# Routes an outside site redirects the owner's browser to, so they arrive as a cross-site
# navigation carrying the Lax cookie: Google's OAuth consent → the Gmail callback.
NAVIGATION_CALLBACKS = frozenset({"/api/settings/gmail/callback"})


def normalize_authority(value: str) -> str | None:
    """`host[:port]` lowercased with the default ports dropped, or None if malformed.
    Both 80 and 443 drop because the scheme is ignored: behind the Cloudflare tunnel the
    api sees plain HTTP for a page the browser loaded over HTTPS."""
    authority = value.strip().lower()
    if not authority or any(c in authority for c in "@/\\ "):
        return None
    host, port = authority, ""
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            return None
        host, rest = authority[: end + 1], authority[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                return None
            port = rest[1:]
    elif ":" in authority:
        host, _, port = authority.rpartition(":")
        if ":" in host:
            return None
    if not host or (port and not port.isdigit()):
        return None
    if port in _DEFAULT_PORTS:
        port = ""
    return f"{host}:{port}" if port else host


def origin_authority(origin: str) -> str | None:
    """The normalized authority of an `Origin` header (`null` and junk → None)."""
    scheme, sep, rest = origin.strip().partition("://")
    if not sep or not scheme or "/" in rest:
        return None
    return normalize_authority(rest)


def origin_matches_host(origin: str, host: str | None) -> bool:
    """Does this Origin name the host the request was sent to? A missing Host header or
    an unparseable value on either side is a mismatch (fail closed)."""
    if host is None:
        return False
    theirs = origin_authority(origin)
    ours = normalize_authority(host)
    return theirs is not None and theirs == ours


def refusal_reason(headers: Headers, method: str | None, path: str) -> str | None:
    """Why a cookie-bearing request must be refused, or None to let it through. `method`
    is None for a WebSocket handshake, which never gets the navigation exemption."""
    site = headers.get("sec-fetch-site")
    if site is not None:
        site = site.strip().lower()
        # Only the two values that mean "this origin" pass; anything unrecognised is foreign.
        if site in ("same-origin", "none"):
            return None
        # The api serves no HTML: links into the PWA land on Caddy's static shell, never
        # here. So a cross-site top-level navigation is legitimate only on a callback an
        # outside service redirects the browser to. Same-site gets no exemption at all: a
        # preview page can open a popup onto a side-effecting GET just as easily.
        navigates = headers.get("sec-fetch-mode", "").lower() == "navigate"
        if (
            site == "cross-site"
            and method in ("GET", "HEAD")
            and navigates
            and path in NAVIGATION_CALLBACKS
        ):
            return None
        return f"sec-fetch-site={site}"
    origin = headers.get("origin")
    if origin is None:
        return None
    if not origin_matches_host(origin, headers.get("host")):
        return "origin-mismatch"
    return None


PREVIEW_PREFIX = "/__jcode_preview/"
# GETs whose handlers authenticate by a token in the URL and never read the session cookie.
# The owner opening one from webmail sends the Lax cookie on a cross-site navigation, and
# there is nothing for the guard to protect. Kept in step with the route matrix's
# COOKIE_EXEMPT by a test, so a cookie-reading route can't be waved through by accident.
_COOKIE_FREE_GET_PATHS = frozenset({"/api/feed/appointments.ics"})
_COOKIE_FREE_GET_PREFIXES = ("/api/research-share/", "/api/jlaunch-share/", "/api/mc-share/")


def cookie_free_get(path: str) -> bool:
    return path in _COOKIE_FREE_GET_PATHS or path.startswith(_COOKIE_FREE_GET_PREFIXES)


class SameSiteGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        # The jcode preview proxy reads no cookie and strips it before the sandbox; a cookie
        # planted on the parent domain by one preview must not 403 every preview load.
        if scope["path"].startswith(PREVIEW_PREFIX):
            await self.app(scope, receive, send)
            return
        settings = cast(Settings, scope["app"].state.settings)
        headers = Headers(scope=scope)
        cookies = cookie_parser(headers.get("cookie", ""))
        if not any(name in cookies for name in session_cookie_names(settings)):
            await self.app(scope, receive, send)
            return
        is_ws = scope["type"] == "websocket"
        method = None if is_ws else scope["method"]
        if method in ("GET", "HEAD") and cookie_free_get(scope["path"]):
            await self.app(scope, receive, send)
            return
        # An origin the owner listed in DASHBOARD_ALLOWED_ORIGINS is trusted for the
        # WebSocket feeds by name; `origin_allowed` then applies the same list at the route.
        if is_ws and headers.get("origin") in settings.allowed_ws_origins:
            await self.app(scope, receive, send)
            return
        if refusal_reason(headers, method, scope["path"]) is not None:
            if is_ws:
                await send({"type": "websocket.close", "code": 4403, "reason": ""})
            else:
                await _send_refusal(send)
            return
        if is_ws:
            await self.app(scope, receive, send)
            return
        # The cookie-migration marker is written by the auth dependency into the shared
        # scope state; holding the same dict lets the response side read it back.
        state = scope.setdefault("state", {})

        async def send_with_migration(message: Message) -> None:
            if message["type"] == "http.response.start":
                apply_migration(state, MutableHeaders(scope=message), settings)
            await send(message)

        await self.app(scope, receive, send_with_migration)


async def _send_refusal(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_REFUSED)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _REFUSED})
