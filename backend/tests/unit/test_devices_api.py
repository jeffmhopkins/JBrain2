"""Owner-only device-management API with a fake device repo."""

import asyncio
import uuid
from collections.abc import Iterator
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api.deps import current_principal
from jbrain.auth import service as auth_service
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeDeviceRepo


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeDeviceRepo]]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    repo = FakeDeviceRepo()
    auth_repo = FakeAuthRepo()
    with TestClient(app) as c:
        app.state.auth_repo = auth_repo
        app.state.device_repo = repo
        key = asyncio.run(auth_service.rotate_owner_key(auth_repo))
        assert (
            c.post("/api/auth/session", json={"owner_key": key, "device_label": "t"}).status_code
            == 204
        )
        yield c, repo


def test_provision_returns_the_key_once_and_lists_the_device(
    client: tuple[TestClient, FakeDeviceRepo],
) -> None:
    c, repo = client
    resp = c.post("/api/devices", json={"label": "Jeff's iPhone"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["key"]  # shown exactly once
    assert body["device"]["label"] == "Jeff's iPhone"
    assert body["device"]["revoked"] is False
    # The plaintext key is never persisted — only its hash.
    assert body["key"] not in repo.key_hashes.values()

    listed = c.get("/api/devices").json()
    assert [d["id"] for d in listed] == [body["device"]["id"]]
    assert "key" not in listed[0]  # the list never carries key material


def test_rotate_returns_a_new_key_and_revoke_then_404(
    client: tuple[TestClient, FakeDeviceRepo],
) -> None:
    c, _ = client
    device = c.post("/api/devices", json={"label": "phone"}).json()["device"]
    first = c.post("/api/devices", json={"label": "phone"}).json()["key"]

    rotated = c.post(f"/api/devices/{device['id']}/rotate")
    assert rotated.status_code == 200
    assert rotated.json()["key"] and rotated.json()["key"] != first

    assert c.post(f"/api/devices/{device['id']}/revoke").status_code == 204
    # Unknown ids 404 on both rotate and revoke.
    assert c.post(f"/api/devices/{uuid.uuid4()}/rotate").status_code == 404
    assert c.post(f"/api/devices/{uuid.uuid4()}/revoke").status_code == 404


def test_rename_updates_the_label_and_404s_for_unknown(
    client: tuple[TestClient, FakeDeviceRepo],
) -> None:
    c, _ = client
    device = c.post("/api/devices", json={"label": "phone"}).json()["device"]

    renamed = c.post(f"/api/devices/{device['id']}/rename", json={"label": "Jeff's phone"})
    assert renamed.status_code == 204
    assert c.get("/api/devices").json()[0]["label"] == "Jeff's phone"
    # Empty label is rejected by validation; unknown id is a 404.
    assert c.post(f"/api/devices/{device['id']}/rename", json={"label": ""}).status_code == 422
    assert c.post(f"/api/devices/{uuid.uuid4()}/rename", json={"label": "x"}).status_code == 404


def test_delete_removes_the_device_and_404s_for_unknown(
    client: tuple[TestClient, FakeDeviceRepo],
) -> None:
    c, _ = client
    device = c.post("/api/devices", json={"label": "phone"}).json()["device"]

    assert c.delete(f"/api/devices/{device['id']}").status_code == 204
    assert c.get("/api/devices").json() == []
    # A second delete (now unknown) is a 404.
    assert c.delete(f"/api/devices/{device['id']}").status_code == 404


def test_device_routes_are_owner_only(client: tuple[TestClient, FakeDeviceRepo]) -> None:
    c, _ = client
    app = cast(FastAPI, c.app)
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id="cap-1", kind="capability_token", label="scoped"
    )
    try:
        assert c.get("/api/devices").status_code == 403
        assert c.post("/api/devices", json={"label": "x"}).status_code == 403
        assert c.post("/api/devices/whatever/rotate").status_code == 403
        assert c.post("/api/devices/whatever/rename", json={"label": "x"}).status_code == 403
        assert c.delete("/api/devices/whatever").status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_device_routes_require_auth() -> None:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        app.state.device_repo = FakeDeviceRepo()
        assert anon.get("/api/devices").status_code == 401
        assert anon.post("/api/devices", json={"label": "x"}).status_code == 401
