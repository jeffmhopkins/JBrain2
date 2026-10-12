"""The public preview proxy: forwards <slug>-preview.<host> → the control server's
/preview/{slug}, enforces the preview origin in-process, adds the api↔jcode bearer, and
strips the owner's credentials so a sandbox-run dev app never sees them (Wave P3a of
docs/archive/JCODE_PREVIEW_HOST_PLAN.md)."""

from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace
from typing import cast
from urllib.parse import unquote

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from jbrain.api import jcode_preview

_SLUG = "deadbeefdeadbeef"  # 16 hex chars — the control server's token_hex(8) shape
_HOST = f"{_SLUG}-preview.box.test"  # the only origin this preview may be served on


def _client(
    handler=None,
    *,
    jcode_url: str = "http://jcode:9100",
    base_host: str = "box.test",
    host: str = _HOST,
) -> TestClient:
    app = FastAPI()
    app.include_router(jcode_preview.router)
    app.state.settings = SimpleNamespace(
        jcode_url=jcode_url, jcode_token="tok", jcode_preview_base_host=base_host
    )
    if handler is not None:
        app.state.jcode_preview_transport = httpx.MockTransport(handler)
    return TestClient(app, base_url=f"http://{host}")


def test_malformed_slug_is_404() -> None:
    assert _client().get("/__jcode_preview/NOTHEX/").status_code == 404


def test_unconfigured_jcode_or_base_host_is_404() -> None:
    assert _client(jcode_url="").get(f"/__jcode_preview/{_SLUG}/").status_code == 404
    assert _client(base_host="").get(f"/__jcode_preview/{_SLUG}/").status_code == 404


def test_wrong_origin_is_404_even_with_a_valid_slug() -> None:
    # The whole isolation model: a preview is reachable ONLY on its own subdomain. A hit
    # on the main host (or any other) 404s in-process, not trusting the edge.
    assert _client(host="jbrain.box.test").get(f"/__jcode_preview/{_SLUG}/").status_code == 404
    # And the path's slug must match the host's slug — no replay on another preview host.
    other = "aaaabbbbccccdddd"
    assert (
        _client(host=f"{other}-preview.box.test").get(f"/__jcode_preview/{_SLUG}/").status_code
        == 404
    )


def test_forwards_with_bearer_and_strips_owner_credentials() -> None:
    seen: dict[str, str | None] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["cookie"] = req.headers.get("cookie")
        return httpx.Response(200, content=b"<html>app</html>", headers={"set-cookie": "x=1"})

    resp = _client(handler).get(
        f"/__jcode_preview/{_SLUG}/assets/app.js?v=1",
        headers={"cookie": "jbrain_session=secret", "authorization": "Bearer browser"},
    )
    assert resp.status_code == 200
    assert resp.content == b"<html>app</html>"
    assert seen["url"] == f"http://jcode:9100/preview/{_SLUG}/assets/app.js?v=1"
    # The hop carries the derived preview bearer, NOT the browser's; the owner cookie is gone.
    assert seen["auth"] == f"Bearer {jcode_preview.preview_token('tok')}"
    assert seen["cookie"] is None
    # A sandbox dev app can't set a cookie on the owner's browser for the preview origin.
    assert "set-cookie" not in {k.lower() for k in resp.headers}


def test_control_server_unreachable_is_502() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    assert _client(handler).get(f"/__jcode_preview/{_SLUG}/").status_code == 502


def test_oversized_request_body_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jcode_preview, "_MAX_BODY", 10)
    resp = _client().post(f"/__jcode_preview/{_SLUG}/upload", content=b"x" * 100)
    assert resp.status_code == 413


def test_preview_ws_wrong_origin_closes_before_connecting() -> None:
    # The HMR WS shares the HTTP route's origin gate: a valid slug on the wrong Host
    # (or a malformed slug) closes 4404 before any upstream connect.
    client = _client(host="jbrain.box.test")
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(f"/__jcode_preview/{_SLUG}/"),
    ):
        pass
    assert exc.value.code == 4404


def test_preview_ws_malformed_slug_closes() -> None:
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        _client().websocket_connect("/__jcode_preview/NOTHEX/"),
    ):
        pass
    assert exc.value.code == 4404


def _recording(calls: list[str]):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, content=b"upstream reached")

    return handler


async def _raw_status(client: TestClient, raw_path: bytes) -> int:
    # TestClient decodes the path twice and httpx resolves literal dot segments before
    # sending, so neither delivers what a real server hands the app. Build the scope the
    # way uvicorn does: the raw request path, percent-decoded exactly once.
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": unquote(raw_path.decode("latin-1")),
        "raw_path": raw_path,
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", _HOST.encode())],
        "client": ("127.0.0.1", 1),
        "server": (_HOST, 80),
    }
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await client.app(scope, receive, send)  # type: ignore[arg-type]
    return cast(int, sent[0]["status"])


@pytest.mark.parametrize(
    "raw",
    [
        "../../sessions",
        "a/../../../sessions",
        "..",
        "./x",
        "%2e%2e/%2e%2e/sessions",
        "%2E%2e/%2e%2E/sessions",
        "..;/..;/sessions",
        "..%5c..%5csessions",
        "a\\..\\..\\sessions",
        "%252e%252e/%252e%252e/sessions",
        "%25252e%25252e/sessions",
        "a%252f..%252fsessions",
        "a%255c..%255csessions",
        "a%00b",
        # A decoded `?`/`#` would split the upstream URL after the check ran.
        "..%3F/..%3F/sessions",
        "..%23/..%23/sessions",
        "a%09b",
        "a%0d%0ab",
    ],
)
async def test_traversal_is_404_without_contacting_upstream(raw: str) -> None:
    calls: list[str] = []
    client = _client(_recording(calls))
    assert await _raw_status(client, f"/__jcode_preview/{_SLUG}/{raw}".encode()) == 404
    assert calls == []


def test_traversal_through_the_test_client_is_404() -> None:
    calls: list[str] = []
    resp = _client(_recording(calls)).get(f"/__jcode_preview/{_SLUG}/%2e%2e/%2e%2e/sessions")
    assert resp.status_code == 404
    assert calls == []


async def test_final_url_outside_the_session_prefix_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The segment check is the cheap filter; the normalised-URL prefix check is the guard
    # that holds even if the filter misses a variant.
    monkeypatch.setattr(jcode_preview, "_path_is_contained", lambda _p: True)
    calls: list[str] = []
    client = _client(_recording(calls))
    assert await _raw_status(client, f"/__jcode_preview/{_SLUG}/../../sessions".encode()) == 404
    assert calls == []


@pytest.mark.parametrize(
    "path",
    [
        "main.js",
        "node_modules/.vite/deps/react.js?v=1a2b",
        ".well-known/appspecific/x.json",
        "src/a%20b/c.tsx",
        "a/b/c/d.e.f.js?next=../../x",
        "...",
        "..foo/bar..",
        "",
    ],
)
def test_dev_server_paths_still_proxy(path: str) -> None:
    calls: list[str] = []
    resp = _client(_recording(calls)).get(f"/__jcode_preview/{_SLUG}/{path}")
    assert resp.status_code == 200
    # An empty query still renders a bare trailing "?" upstream; the path is what matters.
    assert [c.removesuffix("?") for c in calls] == [f"http://jcode:9100/preview/{_SLUG}/{path}"]


def test_preview_hop_carries_the_derived_preview_token_not_the_control_token() -> None:
    # The control server's management API takes the main token; its preview routes take
    # only this derived one, so even a request that escaped the preview prefix would carry
    # nothing the management API accepts.
    seen: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200)

    _client(handler).get(f"/__jcode_preview/{_SLUG}/")
    expected = hmac.new(b"tok", b"jcode-preview", hashlib.sha256).hexdigest()
    assert jcode_preview.preview_token("tok") == expected
    assert seen == [f"Bearer {expected}"]


def test_chunked_body_past_the_cap_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # No Content-Length: the cap has to count the bytes as they arrive.
    monkeypatch.setattr(jcode_preview, "_MAX_BODY", 10)
    calls: list[str] = []

    def chunks():
        for _ in range(10):
            yield b"x" * 5

    resp = _client(_recording(calls)).post(f"/__jcode_preview/{_SLUG}/upload", content=chunks())
    assert resp.status_code == 413
    assert calls == []


def test_body_at_the_cap_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jcode_preview, "_MAX_BODY", 10)
    seen: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.content)
        return httpx.Response(200)

    resp = _client(handler).post(f"/__jcode_preview/{_SLUG}/upload", content=b"x" * 10)
    assert resp.status_code == 200
    assert seen == [b"x" * 10]


@pytest.mark.parametrize(
    "raw", ["%2e%2e/%2e%2e/sessions", "a/%2E%2e/x", "..;/x", "a%5c..%5cx", "..%3F/..%3F/sessions"]
)
def test_preview_ws_traversal_closes_before_connecting(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxied: list[str] = []

    async def fake_proxy(ws: WebSocket, _settings: object, _slug: str, path: str) -> None:
        proxied.append(path)
        await ws.close(code=1000)

    monkeypatch.setattr(jcode_preview, "_proxy_ws", fake_proxy)
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        # An absolute URL: websocket_connect otherwise sends Host: testserver, which the
        # origin gate refuses before the path is ever looked at.
        _client().websocket_connect(f"ws://{_HOST}/__jcode_preview/{_SLUG}/{raw}"),
    ):
        pass
    assert exc.value.code == 4404
    assert proxied == []


def test_preview_ws_dotted_path_still_bridges(monkeypatch: pytest.MonkeyPatch) -> None:
    proxied: list[str] = []

    async def fake_proxy(ws: WebSocket, _settings: object, _slug: str, path: str) -> None:
        proxied.append(path)
        await ws.close(code=1000)

    monkeypatch.setattr(jcode_preview, "_proxy_ws", fake_proxy)
    with (
        pytest.raises(WebSocketDisconnect),
        _client().websocket_connect(f"ws://{_HOST}/__jcode_preview/{_SLUG}/node_modules/.vite/hmr"),
    ):
        pass
    assert proxied == ["node_modules/.vite/hmr"]
