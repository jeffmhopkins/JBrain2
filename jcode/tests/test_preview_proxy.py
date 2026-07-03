"""The host-mode dev-server reverse-proxy: forwards to a loopback dev server, rewrites
the Host so an allowlisting framework accepts it, and 502s when nothing's listening
(Wave P2 of docs/archive/JCODE_PREVIEW_HOST_PLAN.md). Driven by an httpx mock transport
— no real socket."""

from __future__ import annotations

import httpx
from starlette.requests import Request

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
