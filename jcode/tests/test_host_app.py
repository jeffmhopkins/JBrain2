"""The control surface in host preview mode: per-session hostname reservation, its
lifecycle (survives a pause, freed on delete), and the reverse-proxy route's guards
(Wave P2 of docs/archive/JCODE_PREVIEW_HOST_PLAN.md)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jcode_ctl.app import create_app
from jcode_ctl.config import Settings
from jcode_ctl.host_preview import HostPreviewManager
from jcode_ctl.sessions import SessionManager
from jcode_ctl.workspace import FakeWorkspace

AUTH = {"Authorization": "Bearer t"}


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
    assert c.get("/preview/deadbeefdeadbeef/", headers=AUTH).status_code == 404


def test_proxy_paused_session_is_404() -> None:
    c = _host_app()
    sid = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    url = c.post(f"/sessions/{sid}/preview", json={}, headers=AUTH).json()["url"]
    c.post(f"/sessions/{sid}/stop", headers=AUTH)
    # A paused session is never reachable, even though its slug still resolves.
    assert c.get(f"/preview/{_slug(url)}/", headers=AUTH).status_code == 404


def test_proxy_running_session_without_a_dev_server_is_502() -> None:
    c = _host_app()
    sid = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    url = c.post(f"/sessions/{sid}/preview", json={}, headers=AUTH).json()["url"]
    # Slug resolves, session is running, but nothing is listening on the reserved port →
    # a bad-gateway, not a crash.
    assert c.get(f"/preview/{_slug(url)}/", headers=AUTH).status_code == 502


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
        c.websocket_connect(
            "/preview/deadbeefdeadbeef/", headers={"authorization": "Bearer t"}
        ),
    ):
        pass
    assert exc.value.code == 4404
