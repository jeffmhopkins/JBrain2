"""The owner's world and backup routes (MINECRAFT_BEDROCK_PLAN §M2/§M3).

Pinned: an upload streams through to the sidecar with its length and name and is never
held by the api; a download streams back as an attachment; a sidecar refusal (a busy
lifecycle, an invalid world) arrives as what it is; path parameters can't smuggle a
traversal; server-wide settings are a fixed set mapped onto server.properties keys.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import minecraft as api
from jbrain.api.deps import owner_only
from jbrain.minecraft import client as mc

SETTINGS: Any = SimpleNamespace(minecraft_url="http://mc", minecraft_token="tok")


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch):
    seen: list[httpx.Request] = []
    replies: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return replies.get(request.url.path, httpx.Response(200, json={"ok": True}))

    monkeypatch.setattr(mc, "_transport", httpx.MockTransport(handler))
    return seen, replies


@pytest.fixture
def client(sidecar) -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[owner_only] = lambda: SimpleNamespace(id="owner")
    app.state.settings = SETTINGS
    return TestClient(app)


def test_an_upload_streams_through_with_its_length_and_name(client, sidecar) -> None:
    seen, _ = sidecar
    blob = b"PK\x03\x04" + b"x" * 5000
    resp = client.post(
        "/api/minecraft/worlds/slot2/import?name=Castle",
        content=blob,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200, resp.text
    up = seen[-1]
    assert up.url.path == "/worlds/slot2/import"
    assert up.url.params["name"] == "Castle"
    assert up.headers["authorization"] == "Bearer tok"
    assert up.content == blob


def test_an_invalid_world_is_a_400_with_the_sidecars_reason(client, sidecar) -> None:
    _, replies = sidecar
    replies["/worlds/slot2/import"] = httpx.Response(
        400, json={"detail": "that isn't a Bedrock world (no level.dat inside)"}
    )
    resp = client.post("/api/minecraft/worlds/slot2/import", content=b"nope")
    assert resp.status_code == 400 and "level.dat" in resp.json()["detail"]


def test_a_busy_server_refuses_with_409(client, sidecar) -> None:
    _, replies = sidecar
    replies["/worlds/slot3/load"] = httpx.Response(409, json={"detail": "busy: updating"})
    resp = client.post("/api/minecraft/worlds/slot3/load")
    assert resp.status_code == 409 and "updating" in resp.json()["detail"]


def test_a_download_streams_back_as_an_attachment(client, sidecar) -> None:
    _, replies = sidecar
    name = "world-20261010-120000-mine.mcworld"
    replies[f"/snapshots/{name}/file"] = httpx.Response(
        200, content=b"zipbytes", headers={"content-length": "8"}
    )
    resp = client.get(f"/api/minecraft/backups/{name}/file")
    assert resp.status_code == 200 and resp.content == b"zipbytes"
    assert name in resp.headers["content-disposition"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/minecraft/worlds/..%2Fetc/load",
        "/api/minecraft/worlds/world/load",
        "/api/minecraft/backups/..%2F..%2Fsecret/file",
        "/api/minecraft/backups/notabackup.zip/file",
    ],
)
def test_path_params_cannot_smuggle_a_traversal(client, sidecar, path: str) -> None:
    seen, _ = sidecar
    method = client.post if path.endswith("/load") else client.get
    assert method(path).status_code in (404, 422)
    assert seen == []


def test_restore_targets_a_slot_and_reset_carries_its_mode(client, sidecar) -> None:
    seen, _ = sidecar
    name = "world-20261010-120000-mine.mcworld"
    client.post(f"/api/minecraft/backups/{name}/restore", json={"slot": "slot3"})
    client.post("/api/minecraft/worlds/slot2/reset", json={"mode": "same_seed"})
    assert json.loads(seen[0].content) == {"slot": "slot3"}
    assert json.loads(seen[1].content) == {"mode": "same_seed", "seed": None}


def test_server_settings_are_a_fixed_set_on_server_properties(client, sidecar) -> None:
    seen, replies = sidecar
    replies["/properties"] = httpx.Response(
        200,
        json={
            "effective": {
                "server-name": "JBrain",
                "max-players": "10",
                "view-distance": "32",
                "level-name": "world",
            }
        },
    )
    resp = client.put(
        "/api/minecraft/server-settings", json={"server_name": "Hopkins", "max_players": 6}
    )
    assert resp.status_code == 200
    assert (resp.json()["max_players"], resp.json()["view_distance"]) == (10, 32)  # numbers
    sent = next(r for r in seen if r.method == "POST" and r.url.path == "/properties")
    assert json.loads(sent.content) == {"set": {"server-name": "Hopkins", "max-players": "6"}}
    assert (
        client.put("/api/minecraft/server-settings", json={"max_players": 500}).status_code == 422
    )


def test_rules_and_seed_routes_pass_through(client, sidecar) -> None:
    seen, replies = sidecar
    replies["/worlds/new-seed"] = httpx.Response(200, json={"seed": "123"})
    assert client.get("/api/minecraft/worlds/new-seed").json() == {"seed": "123"}
    client.put("/api/minecraft/worlds/slot2/rules", json={"set": {"doFireTick": False}})
    sent = next(r for r in seen if r.url.path == "/worlds/slot2/rules")
    assert json.loads(sent.content) == {"set": {"doFireTick": False}}
    client.post(
        "/api/minecraft/worlds/slot3/create",
        json={"name": "Peaceful", "seed": "glacier", "rules": {"keepInventory": True}},
    )
    created = next(r for r in seen if r.url.path == "/worlds/slot3/create")
    assert json.loads(created.content)["rules"] == {"keepInventory": True}
    assert (
        client.post("/api/minecraft/worlds/slot3/create", json={"seed": "x" * 65}).status_code
        == 422
    )
