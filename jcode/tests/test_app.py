"""The control HTTP surface: auth gate + the session command set, all faked."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_healthz_is_open(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_every_command_requires_the_token(client: TestClient) -> None:
    assert client.get("/sessions").status_code == 401
    assert client.post("/sessions", json={"repo": "r"}).status_code == 401
    assert client.post("/sessions/x/turn", json={"prompt": "hi"}).status_code == 401


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


def test_turn_streams_sse_frames(client: TestClient, auth: dict[str, str]) -> None:
    sid = client.post("/sessions", json={"repo": "r"}, headers=auth).json()["id"]
    resp = client.post(
        f"/sessions/{sid}/turn", json={"prompt": "add a button"}, headers=auth
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1]["type"] == "done"
    assert any(e["type"] == "tool_use" for e in events)


def test_reset_and_delete(client: TestClient, auth: dict[str, str]) -> None:
    sid = client.post("/sessions", json={"repo": "r"}, headers=auth).json()["id"]
    reset = client.post(f"/sessions/{sid}/reset", headers=auth)
    assert reset.json()["status"] == "ready"
    assert client.delete(f"/sessions/{sid}", headers=auth).status_code == 204
    assert client.get(f"/sessions/{sid}", headers=auth).status_code == 404
