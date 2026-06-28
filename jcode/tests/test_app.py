"""The control HTTP surface: auth gate + the session command set, all faked."""

from __future__ import annotations

from fastapi.testclient import TestClient

from jcode_ctl.app import create_app
from jcode_ctl.config import Settings
from jcode_ctl.preview import FakeTunnel, PreviewManager
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


def test_preview_open_status_close(client: TestClient, auth: dict[str, str]) -> None:
    sid = client.post("/sessions", json={"repo": "r"}, headers=auth).json()["id"]
    assert client.get(f"/sessions/{sid}/preview", headers=auth).json() == {
        "enabled": True,
        "url": None,
        "mode": "tunnel",
    }
    opened = client.post(f"/sessions/{sid}/preview", json={}, headers=auth).json()
    assert opened["url"].endswith(".trycloudflare.com")
    assert opened["mode"] == "tunnel"
    assert (
        client.get(f"/sessions/{sid}/preview", headers=auth).json()["url"]
        == opened["url"]
    )
    assert client.delete(f"/sessions/{sid}/preview", headers=auth).status_code == 204
    assert client.get(f"/sessions/{sid}/preview", headers=auth).json()["url"] is None


def test_deleting_a_session_closes_its_preview(
    client: TestClient, auth: dict[str, str]
) -> None:
    sid = client.post("/sessions", json={"repo": "r"}, headers=auth).json()["id"]
    client.post(f"/sessions/{sid}/preview", json={}, headers=auth)
    assert client.delete(f"/sessions/{sid}", headers=auth).status_code == 204
    # The session is gone, so even its preview status 404s on the session lookup.
    assert client.get(f"/sessions/{sid}/preview", headers=auth).status_code == 404


def test_stopping_a_session_closes_its_preview(
    client: TestClient, auth: dict[str, str]
) -> None:
    # A paused session must not keep a live tunnel: cloudflared isn't a sandbox process,
    # so leaked tunnels stack up and TryCloudflare rate-limits new ones into oblivion.
    sid = client.post("/sessions", json={"repo": "r"}, headers=auth).json()["id"]
    client.post(f"/sessions/{sid}/preview", json={}, headers=auth)
    assert client.post(f"/sessions/{sid}/stop", headers=auth).status_code == 200
    # The session lives on (it's only paused), but its tunnel is gone.
    assert client.get(f"/sessions/{sid}/preview", headers=auth).json()["url"] is None


def test_resetting_a_session_closes_its_preview(
    client: TestClient, auth: dict[str, str]
) -> None:
    sid = client.post("/sessions", json={"repo": "r"}, headers=auth).json()["id"]
    client.post(f"/sessions/{sid}/preview", json={}, headers=auth)
    assert client.post(f"/sessions/{sid}/reset", headers=auth).status_code == 200
    assert client.get(f"/sessions/{sid}/preview", headers=auth).json()["url"] is None


def test_preview_disabled_is_409() -> None:
    mgr = SessionManager(FakeWorkspace(), "/work")
    app = create_app(
        Settings(token="t"), mgr, PreviewManager(FakeTunnel, enabled=False)
    )
    c = TestClient(app)
    h = {"Authorization": "Bearer t"}
    sid = c.post("/sessions", json={"repo": "r"}, headers=h).json()["id"]
    assert c.get(f"/sessions/{sid}/preview", headers=h).json()["enabled"] is False
    assert c.post(f"/sessions/{sid}/preview", json={}, headers=h).status_code == 409
