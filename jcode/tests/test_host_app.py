"""The control surface in host preview mode: per-session hostname reservation, its
lifecycle (survives a pause, freed on delete), and the reverse-proxy route's guards
(Wave P2 of docs/archive/JCODE_PREVIEW_HOST_PLAN.md)."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jcode_ctl.app import create_app, preview_token
from jcode_ctl.config import Settings
from jcode_ctl.host_preview import HostPreviewManager
from jcode_ctl.sessions import SessionManager
from jcode_ctl.workspace import FakeWorkspace

AUTH = {"Authorization": "Bearer t"}
# The preview routes' own credential: the api derives the same value from JCODE_TOKEN.
PREVIEW_AUTH = {"Authorization": f"Bearer {preview_token('t')}"}


def _host_app() -> TestClient:
    sessions = SessionManager(FakeWorkspace(), "/work")
    # A high, almost-certainly-closed port pool so the proxy's "no dev server" path hits
    # a real connection refusal (→ 502) without colliding with anything CI is running.
    host = HostPreviewManager(base_host="box.test", port_low=59000, port_high=59010)
    settings = Settings(token="t", preview_base_host="box.test")
    app = create_app(settings, sessions, host)
    return TestClient(app)


def _slug(url: str) -> str:
    # https://<slug>-preview.box.test → <slug>
    return url.removeprefix("https://").removesuffix("-preview.box.test")


def test_open_reports_a_stable_per_session_hostname() -> None:
    c = _host_app()
    sid = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    opened = c.post(f"/sessions/{sid}/preview", json={}, headers=AUTH).json()
    url = opened["url"]
    assert url.startswith("https://") and url.endswith("-preview.box.test")
    # The GUI keys off `mode` to reuse the one Preview tab; host reports the dev port.
    assert opened["mode"] == "host"
    assert opened["port"] == 59000  # first of the test pool
    # Idempotent + reported by status (the hostname is the session's for its life).
    status = c.get(f"/sessions/{sid}/preview", headers=AUTH).json()
    assert status["url"] == url
    assert status["mode"] == "host" and status["port"] == 59000
    assert (
        c.post(f"/sessions/{sid}/preview", json={}, headers=AUTH).json()["url"] == url
    )


def test_hostname_survives_a_pause_but_not_a_delete() -> None:
    c = _host_app()
    sid = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    url = c.post(f"/sessions/{sid}/preview", json={}, headers=AUTH).json()["url"]
    # A stop pauses the session; host mode keeps the reservation (unlike a tunnel), so a
    # restart resumes on the same URL.
    c.post(f"/sessions/{sid}/stop", headers=AUTH)
    assert c.get(f"/sessions/{sid}/preview", headers=AUTH).json()["url"] == url
    # Delete frees it: the session (and its lookup) is gone.
    assert c.delete(f"/sessions/{sid}", headers=AUTH).status_code == 204
    assert c.get(f"/sessions/{sid}/preview", headers=AUTH).status_code == 404


def test_proxy_unknown_slug_is_404() -> None:
    c = _host_app()
    assert c.get("/preview/deadbeefdeadbeef/", headers=PREVIEW_AUTH).status_code == 404


def test_proxy_paused_session_is_404() -> None:
    c = _host_app()
    sid = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    url = c.post(f"/sessions/{sid}/preview", json={}, headers=AUTH).json()["url"]
    c.post(f"/sessions/{sid}/stop", headers=AUTH)
    # A paused session is never reachable, even though its slug still resolves.
    assert c.get(f"/preview/{_slug(url)}/", headers=PREVIEW_AUTH).status_code == 404


def test_proxy_running_session_without_a_dev_server_is_502() -> None:
    c = _host_app()
    sid = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    url = c.post(f"/sessions/{sid}/preview", json={}, headers=AUTH).json()["url"]
    # Slug resolves, session is running, but nothing is listening on the reserved port →
    # a bad-gateway, not a crash.
    assert c.get(f"/preview/{_slug(url)}/", headers=PREVIEW_AUTH).status_code == 502


def test_preview_ws_rejects_a_bad_bearer() -> None:
    # The HMR WS handshake is bearer-authed (the api presents it), like the terminal.
    # The slug here is UNKNOWN: a 4401 (not 4404) proves auth is checked BEFORE slug
    # resolution — the order that keeps an unauthenticated caller from probing slugs.
    c = _host_app()
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        c.websocket_connect(
            "/preview/deadbeefdeadbeef/", headers={"authorization": "Bearer wrong"}
        ),
    ):
        pass
    assert exc.value.code == 4401


def test_preview_ws_unknown_slug_closes_before_connecting() -> None:
    c = _host_app()
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        c.websocket_connect("/preview/deadbeefdeadbeef/", headers=PREVIEW_AUTH),
    ):
        pass
    assert exc.value.code == 4404


def test_preview_token_is_the_hmac_of_the_control_token() -> None:
    # The api mirrors this derivation (jbrain.api.jcode_preview.preview_token); a change
    # on one side alone breaks every preview.
    expected = hmac.new(b"t", b"jcode-preview", hashlib.sha256).hexdigest()
    assert preview_token("t") == expected


def test_preview_routes_refuse_the_control_token() -> None:
    # The api's preview hop never holds the management credential, and the preview
    # routes don't accept it either: each token opens exactly one surface.
    c = _host_app()
    sid = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    url = c.post(f"/sessions/{sid}/preview", json={}, headers=AUTH).json()["url"]
    assert c.get(f"/preview/{_slug(url)}/", headers=AUTH).status_code == 401
    assert c.get(f"/preview/{_slug(url)}", headers=AUTH).status_code == 401
    assert c.get(f"/preview/{_slug(url)}/").status_code == 401
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        c.websocket_connect(f"/preview/{_slug(url)}/", headers=AUTH),
    ):
        pass
    assert exc.value.code == 4401


def test_management_routes_refuse_the_preview_token() -> None:
    # The preview token is what a request smuggled out of /preview/{slug} would carry;
    # it must open nothing else.
    c = _host_app()
    sid = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    assert c.get("/sessions", headers=PREVIEW_AUTH).status_code == 401
    assert c.get(f"/sessions/{sid}", headers=PREVIEW_AUTH).status_code == 401
    assert (
        c.post("/sessions", json={"repo": "r"}, headers=PREVIEW_AUTH).status_code == 401
    )
    assert c.delete(f"/sessions/{sid}", headers=PREVIEW_AUTH).status_code == 401
    assert c.get(f"/sessions/{sid}/preview", headers=PREVIEW_AUTH).status_code == 401
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        c.websocket_connect(f"/sessions/{sid}/terminal", headers=PREVIEW_AUTH),
    ):
        pass
    assert exc.value.code == 4401


def test_proxy_refuses_a_dot_segment_path() -> None:
    # Defence in depth behind the api's own check: a dot segment never reaches the dev
    # server (which would resolve it against its own root).
    c = _host_app()
    sid = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    url = c.post(f"/sessions/{sid}/preview", json={}, headers=AUTH).json()["url"]
    resp = c.get(f"/preview/{_slug(url)}/%2e%2e/%2e%2e/etc", headers=PREVIEW_AUTH)
    assert resp.status_code == 404
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        c.websocket_connect(f"/preview/{_slug(url)}/a/%2e%2e/x", headers=PREVIEW_AUTH),
    ):
        pass
    assert exc.value.code == 4404
