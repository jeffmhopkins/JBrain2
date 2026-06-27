"""The on-box favicon fetcher for web citation chips (docs/ASSISTANT.md "Agent
selection"). HTTP is faked via MockTransport — no live network, like the rest of
the jerv web clients; the SSRF DNS check is skipped under an injected transport."""

import httpx

from jbrain.web.favicon import FaviconFetcher, normalize_host

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_ICO = b"\x00\x00\x01\x00" + b"\x00" * 32
_JPEG = b"\xff\xd8\xff" + b"\x00" * 32
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>'

_HOME_WITH_LINK = (
    b"<html><head>"
    b'<link rel="stylesheet" href="/style.css">'
    b'<link rel="shortcut icon" href="/brand/icon.png">'
    b"</head><body>hi</body></html>"
)
_HOME_NO_LINK = b"<html><head><title>x</title></head><body>hi</body></html>"


def _fetcher(handle) -> FaviconFetcher:  # type: ignore[no-untyped-def]
    return FaviconFetcher(transport=httpx.MockTransport(handle))


# --- normalize_host --------------------------------------------------------


def test_normalize_host_reduces_to_bare_hostname() -> None:
    assert normalize_host("https://Example.com/games/store") == "example.com"
    assert normalize_host("EXAMPLE.com:443") == "example.com"
    assert normalize_host("example.com") == "example.com"
    assert normalize_host("") is None
    assert normalize_host("   ") is None


# --- declared <link rel=icon> ----------------------------------------------


async def test_fetch_uses_declared_icon_link() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        html = {"content-type": "text/html"}
        if request.url.path == "/":
            return httpx.Response(200, content=_HOME_WITH_LINK, headers=html)
        if request.url.path == "/brand/icon.png":
            return httpx.Response(200, content=_PNG)
        return httpx.Response(404)

    result = await _fetcher(handle).fetch("https://example.com/games/store")
    assert result is not None
    assert result.content_type == "image/png"
    assert result.data == _PNG


async def test_fetch_falls_back_to_well_known_favicon_ico() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, content=_HOME_NO_LINK, headers={"content-type": "text/html"})
        if request.url.path == "/favicon.ico":
            return httpx.Response(200, content=_ICO)
        return httpx.Response(404)

    result = await _fetcher(handle).fetch("example.com")
    assert result is not None and result.content_type == "image/x-icon"


async def test_fetch_falls_back_when_homepage_unreadable() -> None:
    # No homepage HTML at all — the /favicon.ico fallback still gets a shot.
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/favicon.ico":
            return httpx.Response(200, content=_JPEG)
        return httpx.Response(500)

    result = await _fetcher(handle).fetch("example.com")
    assert result is not None and result.content_type == "image/jpeg"


# --- validation: only recognised raster images -----------------------------


async def test_fetch_refuses_svg_favicon() -> None:
    # SVG can carry script and we serve these bytes back to the PWA — refuse it.
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, content=_HOME_NO_LINK, headers={"content-type": "text/html"})
        if request.url.path == "/favicon.ico":
            return httpx.Response(200, content=_SVG, headers={"content-type": "image/svg+xml"})
        return httpx.Response(404)

    assert await _fetcher(handle).fetch("example.com") is None


async def test_fetch_refuses_non_image_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, content=_HOME_NO_LINK, headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"<!doctype html>not an icon")

    assert await _fetcher(handle).fetch("example.com") is None


async def test_fetch_refuses_oversized_icon() -> None:
    big = _PNG[:8] + b"\x00" * 300_000

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, content=_HOME_NO_LINK, headers={"content-type": "text/html"})
        if request.url.path == "/favicon.ico":
            return httpx.Response(200, content=big)
        return httpx.Response(404)

    assert await _fetcher(handle).fetch("example.com") is None


async def test_fetch_returns_none_for_blank_host() -> None:
    def handle(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        return httpx.Response(200, content=_PNG)

    assert await _fetcher(handle).fetch("") is None
