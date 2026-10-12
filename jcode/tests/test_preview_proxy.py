"""The host-mode dev-server reverse-proxy: forwards to a loopback dev server, rewrites
the Host so an allowlisting framework accepts it, and 502s when nothing's listening
(Wave P2 of docs/archive/JCODE_PREVIEW_HOST_PLAN.md). Driven by an httpx mock transport
— no real socket."""

from __future__ import annotations

import asyncio
import socket
from typing import ClassVar

import httpx
import pytest
from starlette.requests import Request

from jcode_ctl import preview_proxy
from jcode_ctl.preview_proxy import proxy_http


def _request(method: str = "GET", query: str = "", body: bytes = b"") -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "query_string": query.encode(),
        "headers": [(b"x-test", b"1")],
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _factory(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_forwards_path_query_and_rewrites_host_to_localhost() -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["host"] = req.headers["host"]
        return httpx.Response(200, content=b"<html>ok</html>")

    resp = await proxy_http(
        5187, "assets/app.js", _request(query="v=2"), client_factory=_factory(handler)
    )
    assert resp.status_code == 200
    assert resp.body == b"<html>ok</html>"
    # The dev server is addressed on loopback at its reserved port, with the public
    # preview Host rewritten to the localhost value the dev server trusts (#628 lesson).
    assert seen["url"] == "http://127.0.0.1:5187/assets/app.js?v=2"
    assert seen["host"] == "localhost:5187"


async def test_passes_through_status_and_body() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"nope")

    resp = await proxy_http(
        5187, "missing", _request(), client_factory=_factory(handler)
    )
    assert resp.status_code == 404
    assert resp.body == b"nope"


async def test_falls_back_to_ipv6_loopback_when_ipv4_refuses() -> None:
    # Vite & friends default to `localhost`, which Node often binds to ::1 only — so the
    # IPv4 dial refuses and we must retry [::1] before 502'ing.
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        if req.url.host == "127.0.0.1":
            raise httpx.ConnectError("connection refused", request=req)
        return httpx.Response(200, content=b"<html>vite</html>")

    resp = await proxy_http(5187, "", _request(), client_factory=_factory(handler))
    assert resp.status_code == 200
    assert resp.body == b"<html>vite</html>"
    # Tried IPv4 first, then fell back to the IPv6 loopback the dev server bound.
    assert seen[0] == "127.0.0.1"
    assert "::1" in seen[1]


async def test_request_body_survives_the_ipv4_to_ipv6_retry() -> None:
    # The body is read once and reused across attempts — a POST that falls back to ::1
    # must still carry its bytes.
    got: dict[str, bytes] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "127.0.0.1":
            raise httpx.ConnectError("connection refused", request=req)
        got["body"] = req.content
        return httpx.Response(200, content=b"ok")

    resp = await proxy_http(
        5187,
        "submit",
        _request(method="POST", body=b"payload"),
        client_factory=_factory(handler),
    )
    assert resp.status_code == 200
    assert got["body"] == b"payload"


async def test_connect_refused_on_both_families_is_502() -> None:
    tried: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        tried.append(req.url.host)
        raise httpx.ConnectError("connection refused", request=req)

    resp = await proxy_http(5187, "", _request(), client_factory=_factory(handler))
    assert resp.status_code == 502
    # Both loopback families were attempted before giving up.
    assert len(tried) == 2


async def test_does_not_retry_the_other_family_on_a_read_error() -> None:
    # A timeout/malformed reply means the server IS here (connected) — retrying the
    # other family would be wrong; 502 directly.
    tried: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        tried.append(req.url.host)
        raise httpx.ReadTimeout("slow", request=req)

    resp = await proxy_http(5187, "", _request(), client_factory=_factory(handler))
    assert resp.status_code == 502
    assert tried == ["127.0.0.1"]


@pytest.mark.parametrize(
    "path",
    ["../etc/passwd", "a/./b", "..", "..;/x", "a\\..\\b", "%2e%2e/x", "%252e%252e/x"],
)
async def test_dot_segments_never_reach_the_dev_server(path: str) -> None:
    tried: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        tried.append(str(req.url))
        return httpx.Response(200)

    resp = await proxy_http(5187, path, _request(), client_factory=_factory(handler))
    assert resp.status_code == 404
    assert tried == []


@pytest.mark.parametrize(
    "path", ["main.js", "node_modules/.vite/deps/x.js", ".well-known/a", "..."]
)
async def test_dotted_names_still_proxy(path: str) -> None:
    tried: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        tried.append(req.url.path)
        return httpx.Response(200)

    resp = await proxy_http(5187, path, _request(), client_factory=_factory(handler))
    assert resp.status_code == 200
    assert tried == [f"/{path}"]


# --- The container's egress proxy env must never catch loopback (S2.4) ----------------


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        pytest.param(
            "::1",
            marks=pytest.mark.skipif(not _ipv6_loopback(), reason="no IPv6 loopback"),
        ),
    ],
)
async def test_the_dev_server_is_reached_directly_with_the_proxy_env_set(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """jcode runs with HTTP_PROXY pointing at squid, which refuses loopback. NO_PROXY
    here names neither loopback, so the request only arrives if the proxy env is
    ignored — which keeps a dev server bound to `::1` (Vite's default) previewable."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, host, 0)
    port = server.sockets[0].getsockname()[1]
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")  # a proxy that refuses everything
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "api")
    async with server:
        resp = await proxy_http(port, "", _request())
    assert resp.status_code == 200
    assert resp.body == b"ok"


async def test_the_hmr_socket_never_uses_the_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def connect(url: str, **kwargs: object) -> object:
        calls.append({"url": url, **kwargs})
        raise OSError("refused")

    class _Browser:
        scope: ClassVar[dict[str, object]] = {"subprotocols": []}
        closed: int | None = None

        async def close(self, code: int) -> None:
            self.closed = code

    monkeypatch.setattr(preview_proxy.websockets, "connect", connect)
    browser = _Browser()
    await preview_proxy.proxy_ws(browser, 5173, "", "")  # type: ignore[arg-type]
    assert [c["url"] for c in calls] == ["ws://127.0.0.1:5173/", "ws://[::1]:5173/"]
    assert all("proxy" in c and c["proxy"] is None for c in calls)
    assert browser.closed == 1011
