"""The control HTTP surface: auth gate + the session command set, all faked."""

from __future__ import annotations

from fastapi.testclient import TestClient

from jcode_ctl.app import create_app
from jcode_ctl.config import Settings
from jcode_ctl.host_preview import HostPreviewManager
from jcode_ctl.sessions import SessionManager
from jcode_ctl.workspace import FakeWorkspace


def test_healthz_is_open(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_every_command_requires_the_token(client: TestClient) -> None:
    assert client.get("/sessions").status_code == 401
    assert client.post("/sessions", json={"repo": "r"}).status_code == 401
    assert client.post("/sessions/x/stop").status_code == 401


def test_create_list_get(client: TestClient, auth: dict[str, str]) -> None:
    repo = "github.com/me/repo"
    created = client.post("/sessions", json={"repo": repo}, headers=auth)
    assert created.status_code == 201
    sid = created.json()["id"]
    assert created.json()["status"] == "ready"

    listed = client.get("/sessions", headers=auth).json()
    assert [s["id"] for s in listed] == [sid]
    assert client.get(f"/sessions/{sid}", headers=auth).json()["repo"] == repo


def test_unknown_session_is_404(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/sessions/nope", headers=auth).status_code == 404


def test_stop_and_restart(client: TestClient, auth: dict[str, str]) -> None:
    sid = client.post("/sessions", json={"repo": "r"}, headers=auth).json()["id"]
    stopped = client.post(f"/sessions/{sid}/stop", headers=auth)
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"
    # The checkout is kept, so a restart resumes the same session.
    restarted = client.post(f"/sessions/{sid}/restart", headers=auth)
    assert restarted.json()["status"] == "ready"


def test_restart_unknown_session_is_404(
    client: TestClient, auth: dict[str, str]
) -> None:
    assert client.post("/sessions/nope/restart", headers=auth).status_code == 404


def test_reset_and_delete(client: TestClient, auth: dict[str, str]) -> None:
    sid = client.post("/sessions", json={"repo": "r"}, headers=auth).json()["id"]
    reset = client.post(f"/sessions/{sid}/reset", headers=auth)
    assert reset.json()["status"] == "ready"
    assert client.delete(f"/sessions/{sid}", headers=auth).status_code == 204
    assert client.get(f"/sessions/{sid}", headers=auth).status_code == 404


def test_preview_open_status_reports_the_host_url_and_port(
    client: TestClient, auth: dict[str, str]
) -> None:
    sid = client.post("/sessions", json={"repo": "r"}, headers=auth).json()["id"]
    # Before opening: enabled (a base host is configured), but nothing reserved yet.
    assert client.get(f"/sessions/{sid}/preview", headers=auth).json() == {
        "enabled": True,
        "url": None,
        "mode": "host",
        "port": None,
    }
    opened = client.post(f"/sessions/{sid}/preview", json={}, headers=auth).json()
    assert opened["mode"] == "host"
    assert opened["url"].endswith("-preview.box.test")
    assert opened["port"] is not None
    # Status now reports the same reserved URL + port.
    status = client.get(f"/sessions/{sid}/preview", headers=auth).json()
    assert status["url"] == opened["url"]
    assert status["port"] == opened["port"]
    # DELETE is a no-op in host mode — the reservation (stable URL) is kept.
    assert client.delete(f"/sessions/{sid}/preview", headers=auth).status_code == 204
    final = client.get(f"/sessions/{sid}/preview", headers=auth).json()
    assert final["url"] == opened["url"]


def test_stop_and_reset_keep_the_preview_url(
    client: TestClient, auth: dict[str, str]
) -> None:
    # Host mode keeps the per-session reservation across a pause/reset (the proxy gates
    # a stopped session) so the URL is stable — nothing to tear down.
    sid = client.post("/sessions", json={"repo": "r"}, headers=auth).json()["id"]
    url = client.post(f"/sessions/{sid}/preview", json={}, headers=auth).json()["url"]
    assert client.post(f"/sessions/{sid}/stop", headers=auth).status_code == 200
    assert client.get(f"/sessions/{sid}/preview", headers=auth).json()["url"] == url
    assert client.post(f"/sessions/{sid}/restart", headers=auth).status_code == 200
    assert client.post(f"/sessions/{sid}/reset", headers=auth).status_code == 200
    assert client.get(f"/sessions/{sid}/preview", headers=auth).json()["url"] == url


def test_deleting_a_session_frees_its_preview(
    client: TestClient, auth: dict[str, str]
) -> None:
    sid = client.post("/sessions", json={"repo": "r"}, headers=auth).json()["id"]
    client.post(f"/sessions/{sid}/preview", json={}, headers=auth)
    assert client.delete(f"/sessions/{sid}", headers=auth).status_code == 204
    # The session is gone, so even its preview status 404s on the session lookup.
    assert client.get(f"/sessions/{sid}/preview", headers=auth).status_code == 404


def test_preview_disabled_without_a_base_host_is_409() -> None:
    # No base host → the allocator fail-closes (.enabled False): status reports disabled
    # and an open attempt is refused (409 via PreviewError).
    mgr = SessionManager(FakeWorkspace(), "/work")
    app = create_app(Settings(token="t"), mgr, HostPreviewManager(base_host=""))
    c = TestClient(app)
    h = {"Authorization": "Bearer t"}
    sid = c.post("/sessions", json={"repo": "r"}, headers=h).json()["id"]
    assert c.get(f"/sessions/{sid}/preview", headers=h).json()["enabled"] is False
    assert c.post(f"/sessions/{sid}/preview", json={}, headers=h).status_code == 409
