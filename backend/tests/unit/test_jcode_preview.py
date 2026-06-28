"""The public preview proxy: forwards <slug>-preview.<host> → the control server's
/preview/{slug}, enforces the preview origin in-process, adds the api↔jcode bearer, and
strips the owner's credentials so a sandbox-run dev app never sees them (Wave P3a of
docs/JCODE_PREVIEW_HOST_PLAN.md)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
    # The hop carries the api↔jcode bearer, NOT the browser's; the owner cookie is gone.
    assert seen["auth"] == "Bearer tok"
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
