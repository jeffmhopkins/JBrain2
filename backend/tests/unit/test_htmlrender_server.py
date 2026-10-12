"""The htmlrender sidecar's page-level network lock, exercised on its real server code.

The sidecar's network is `internal: true`; inside it, every request a model-authored page
makes is aborted by `page.route`. That route never sees a WebSocket (N5 in
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
        self.routes: list[Any] = []
        self.socket_routes: list[tuple[Any, Any]] = []
        self.content = ""

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append(pattern)

    async def route_web_socket(self, pattern: Any, handler: Any) -> None:
        self.socket_routes.append((pattern, handler))

    async def set_content(self, html: str, **_: Any) -> None:
        self.content = html

    async def screenshot(self, **_: Any) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", 10, 20) + b"\0" * 8


class _Context:
    def __init__(self, page: _Page) -> None:
        self.page = page

    async def new_page(self) -> _Page:
        return self.page

    async def close(self) -> None:
        return None


class _Browser:
    def __init__(self, page: _Page) -> None:
        self.page = page

    async def new_context(self, **_: Any) -> _Context:
        return _Context(self.page)


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


async def test_every_websocket_is_routed_and_closed(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _Page()

    async def browser() -> _Browser:
        return _Browser(page)

    monkeypatch.setattr(server, "_get_browser", browser)
    out = await server.render(server.RenderRequest(html="<p>hi</p>", width=10, height=20))
    assert (out.width, out.height) == (10, 20)
    assert page.routes == ["**/*"]
    [(pattern, handler)] = page.socket_routes
    for url in ("ws://api:8000/internal/pet/say", "wss://example.com/", "ws://127.0.0.1:9/"):
        assert pattern.fullmatch(url)
    socket = _Socket()
    await handler(socket)
    assert socket.closed
