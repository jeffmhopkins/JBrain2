"""The host-mode dev-server reverse-proxy: forwards to a loopback dev server, rewrites
the Host so an allowlisting framework accepts it, and 502s when nothing's listening
(Wave P2 of docs/JCODE_PREVIEW_HOST_PLAN.md). Driven by an httpx mock transport — no
real socket."""

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


async def test_connect_refused_is_502() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    resp = await proxy_http(5187, "", _request(), client_factory=_factory(handler))
    assert resp.status_code == 502
