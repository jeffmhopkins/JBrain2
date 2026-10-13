"""The control surface in host preview mode: per-session hostname reservation, its
lifecycle (survives a pause, freed on delete), and the reverse-proxy route's guards
(Wave P2 of docs/archive/JCODE_PREVIEW_HOST_PLAN.md)."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect

import jcode_ctl.app as app_mod
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


# --- Port ownership (S2.1): sessions share a network namespace, so the preview only
# proxies a port whose every listener is the session's own uid. ---


def _owned_app(owners: dict[int, set[int]]) -> tuple[TestClient, SessionManager, str]:
    sessions = SessionManager(FakeWorkspace(), "/work")
    host = HostPreviewManager(base_host="box.test", port_low=59000, port_high=59010)
    settings = Settings(token="t", preview_base_host="box.test")
    app = create_app(
        settings, sessions, host, port_owners=lambda port: owners.get(port, set())
    )
    c = TestClient(app)
    sid = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    url = c.post(f"/sessions/{sid}/preview", json={}, headers=AUTH).json()["url"]
    return c, sessions, _slug(url)


def _never_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never(*_a: Any) -> Any:
        raise AssertionError("must not proxy")

    monkeypatch.setattr(app_mod, "proxy_http", _never)
    monkeypatch.setattr(app_mod, "proxy_ws", _never)


def test_preview_proxies_a_port_the_session_owns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxied: list[int] = []

    async def _proxy(port: int, path: str, request: Any) -> Response:
        proxied.append(port)
        return Response("dev", status_code=200)

    monkeypatch.setattr(app_mod, "proxy_http", _proxy)
    owners: dict[int, set[int]] = {}
    c, sessions, slug = _owned_app(owners)
    owners[59000] = {sessions.user_for(sessions.list()[0].id).uid}
    resp = c.get(f"/preview/{slug}/", headers=PREVIEW_AUTH)
    assert (resp.status_code, resp.text, proxied) == (200, "dev", [59000])


@pytest.mark.parametrize("foreign", [{20999}, {0}])
def test_preview_refuses_a_port_another_uid_listens_on(
    monkeypatch: pytest.MonkeyPatch, foreign: set[int]
) -> None:
    _never_proxy(monkeypatch)
    owners: dict[int, set[int]] = {}
    c, sessions, slug = _owned_app(owners)
    own = sessions.user_for(sessions.list()[0].id).uid
    # Squatted outright, or shared with a neighbour bound on the other loopback family.
    for held in (foreign, {own, *foreign}):
        owners[59000] = held
        assert c.get(f"/preview/{slug}/", headers=PREVIEW_AUTH).status_code == 404
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            c.websocket_connect(f"/preview/{slug}/", headers=PREVIEW_AUTH),
        ):
            pass
        assert exc.value.code == 4404


def test_preview_with_nothing_listening_is_502_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _never_proxy(monkeypatch)
    c, _, slug = _owned_app({})
    assert c.get(f"/preview/{slug}/", headers=PREVIEW_AUTH).status_code == 502
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        c.websocket_connect(f"/preview/{slug}/", headers=PREVIEW_AUTH),
    ):
        pass
    assert exc.value.code == 1011


def test_rotate_retires_the_old_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    _never_proxy(monkeypatch)
    c, sessions, old = _owned_app({})
    sid = sessions.list()[0].id
    rotated = c.post(f"/sessions/{sid}/preview/rotate", headers=AUTH)
    assert rotated.status_code == 200
    new_url = rotated.json()["url"]
    assert _slug(new_url) != old and rotated.json()["port"] == 59000
    assert c.get(f"/sessions/{sid}/preview", headers=AUTH).json()["url"] == new_url
    # The old slug no longer resolves; the new one reaches the (absent) dev server.
    assert c.get(f"/preview/{old}/", headers=PREVIEW_AUTH).status_code == 404
    assert c.get(f"/preview/{_slug(new_url)}/", headers=PREVIEW_AUTH).status_code == 502


def test_rotate_is_main_token_only_and_404s_an_unknown_session() -> None:
    c = _host_app()
    sid = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    assert c.post(f"/sessions/{sid}/preview/rotate").status_code == 401
    assert (
        c.post(f"/sessions/{sid}/preview/rotate", headers=PREVIEW_AUTH).status_code
        == 401
    )
    assert c.post("/sessions/nope/preview/rotate", headers=AUTH).status_code == 404
    # A session that never opened a preview has nothing to rotate.
    assert c.post(f"/sessions/{sid}/preview/rotate", headers=AUTH).json()["url"] is None


def _held_open_preview_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dev HMR socket that echoes until something ends it."""

    async def _proxy_ws(browser: Any, port: int, path: str, query: str) -> None:
        await browser.accept()
        try:
            while True:
                await browser.send_text(await browser.receive_text())
        except WebSocketDisconnect:
            return

    monkeypatch.setattr(app_mod, "proxy_ws", _proxy_ws)


@pytest.mark.parametrize("retire", ["rotate", "delete"])
def test_an_open_preview_socket_closes_when_its_slug_is_retired(
    monkeypatch: pytest.MonkeyPatch, retire: str
) -> None:
    # A revoked recipient's open HMR socket must not keep streaming the code's updates.
    _held_open_preview_ws(monkeypatch)
    owners: dict[int, set[int]] = {}
    c, sessions, slug = _owned_app(owners)
    sid = sessions.list()[0].id
    owners[59000] = {sessions.user_for(sid).uid}
    with c.websocket_connect(f"/preview/{slug}/", headers=PREVIEW_AUTH) as ws:
        if retire == "rotate":
            assert c.post(f"/sessions/{sid}/preview/rotate", headers=AUTH).is_success
        else:
            assert c.delete(f"/sessions/{sid}", headers=AUTH).status_code == 204
        message = ws.receive()
        assert (message["type"], message.get("code")) == ("websocket.close", 4404)


def test_rotating_another_session_leaves_an_open_preview_socket_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _held_open_preview_ws(monkeypatch)
    owners: dict[int, set[int]] = {}
    c, sessions, slug = _owned_app(owners)
    sid = sessions.list()[0].id
    owners[59000] = {sessions.user_for(sid).uid}
    other = c.post("/sessions", json={"repo": "r"}, headers=AUTH).json()["id"]
    c.post(f"/sessions/{other}/preview", json={}, headers=AUTH)
    with c.websocket_connect(f"/preview/{slug}/", headers=PREVIEW_AUTH) as ws:
        assert c.post(f"/sessions/{other}/preview/rotate", headers=AUTH).is_success
        ws.send_text("ping")
        assert ws.receive_text() == "ping"
