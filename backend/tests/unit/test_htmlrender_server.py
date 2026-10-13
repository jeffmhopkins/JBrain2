"""The htmlrender sidecar's page-level network lock, exercised on its real server code.

The sidecar's network is `internal: true`; inside it, every request a model-authored page
makes is aborted by a route on the browser CONTEXT, so a popup the page opens is covered
too, and the popup itself is closed. That route never sees a WebSocket (N5 in
SECURITY_REVIEW_FIXES_PLAN.md), so the page also carries `connect-src 'none'` and the
sockets are routed and closed. Playwright is not a backend dependency, so the module is
loaded with a stand-in for it and driven against a fake browser.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_SERVER = Path(__file__).resolve().parents[3] / "deploy" / "htmlrender" / "server.py"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Any:
    fake = types.ModuleType("playwright.async_api")
    fake.Browser = object  # type: ignore[attr-defined]
    fake.async_playwright = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake)
    spec = importlib.util.spec_from_file_location("htmlrender_server_under_test", _SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Page:
    def __init__(self) -> None:
        self.content = ""
        self.closed = False

    async def set_content(self, html: str, **_: Any) -> None:
        self.content = html

    async def screenshot(self, **_: Any) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", 10, 20) + b"\0" * 8

    async def close(self) -> None:
        self.closed = True


class _ClosedPage(_Page):
    """A popup that arrives after the context closed: its own close raises."""

    async def close(self) -> None:
        raise RuntimeError("Target page, context or browser has been closed")


class _Context:
    """Records what the server wires on the context, and fires its `page` listeners the way
    Playwright does: for the render's own page as it is created, and for any popup."""

    def __init__(self, page: _Page) -> None:
        self.page = page
        self.routes: list[Any] = []
        self.socket_routes: list[tuple[Any, Any]] = []
        self.page_listeners: list[Any] = []
        self.wired_before_page = False

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def route_web_socket(self, pattern: Any, handler: Any) -> None:
        self.socket_routes.append((pattern, handler))

    def on(self, event: str, handler: Any) -> None:
        assert event == "page"
        self.page_listeners.append(handler)

    async def new_page(self) -> _Page:
        self.wired_before_page = bool(self.routes and self.socket_routes)
        for listener in self.page_listeners:
            await listener(self.page)
        return self.page

    async def open_popup(self, popup: _Page | None = None) -> _Page:
        popup = popup or _Page()
        for listener in self.page_listeners:
            await listener(popup)
        return popup

    async def close(self) -> None:
        return None


class _Browser:
    def __init__(self, page: _Page) -> None:
        self.page = page

    async def new_context(self, **_: Any) -> _Context:
        self.context = _Context(self.page)
        return self.context


class _Socket:
    closed = False

    async def close(self) -> None:
        self.closed = True


def test_the_page_forbids_every_connection_before_the_callers_markup(server: Any) -> None:
    body = server.RenderRequest(html="<script>new WebSocket('ws://api:8000/x')</script>", width=10)
    doc = server._document(body)
    csp = '<meta http-equiv="Content-Security-Policy" content="connect-src \'none\'">'
    assert csp in doc
    # In the head, ahead of the fragment: a meta CSP in the body is ignored, and the caller's
    # markup comes after it, so it can only add a stricter policy, never a looser one.
    assert doc.index(csp) < doc.index("</head>") < doc.index("new WebSocket")


async def _render(server: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Browser]:
    page = _Page()
    fake = _Browser(page)

    async def browser() -> _Browser:
        return fake

    monkeypatch.setattr(server, "_get_browser", browser)
    out = await server.render(server.RenderRequest(html="<p>hi</p>", width=10, height=20))
    return out, fake


async def test_every_websocket_is_routed_and_closed(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, browser = await _render(server, monkeypatch)
    assert (out.width, out.height) == (10, 20)
    context = browser.context
    [(pattern, handler)] = context.socket_routes
    for url in ("ws://api:8000/internal/pet/say", "wss://example.com/", "ws://127.0.0.1:9/"):
        assert pattern.fullmatch(url)
    socket = _Socket()
    await handler(socket)
    assert socket.closed


class _Route:
    def __init__(self, url: str) -> None:
        self.request = types.SimpleNamespace(url=url)
        self.outcome = ""

    async def continue_(self) -> None:
        self.outcome = "continue"

    async def abort(self) -> None:
        self.outcome = "abort"


async def test_requests_are_blocked_for_every_page_in_the_context(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A page-scoped route never saw a `window.open` popup's requests; the route is on the
    # context, and in place before the render's own page exists.
    out, browser = await _render(server, monkeypatch)
    context = browser.context
    assert context.wired_before_page
    [(pattern, block)] = context.routes
    assert pattern == "**/*"
    for url, outcome in (
        ("http://api:8000/internal/x", "abort"),
        ("https://example.com/", "abort"),
        ("data:image/png;base64,AAAA", "continue"),
        ("about:blank", "continue"),
    ):
        route = _Route(url)
        await block(route)
        assert route.outcome == outcome
    # The render itself still produces the page's PNG.
    assert (out.width, out.height) == (10, 20) and out.png_base64


async def test_a_popup_is_closed_but_the_render_page_is_not(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, browser = await _render(server, monkeypatch)
    context = browser.context
    assert not browser.page.closed
    popup = await context.open_popup()
    assert popup.closed
    assert not browser.page.closed


async def test_a_popup_landing_after_close_is_swallowed(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The `page` handler is fire-and-forget; a late popup whose close fails must not raise
    # out of it (an unretrieved task exception is all it would produce).
    _, browser = await _render(server, monkeypatch)
    await browser.context.open_popup(_ClosedPage())
