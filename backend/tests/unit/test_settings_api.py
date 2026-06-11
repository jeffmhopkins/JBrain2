"""The /api/settings surface — the first server-synced user settings — with
the store faked; the real store's SQL semantics are covered in
test_settings_pg."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeSettingsStore]]:
    app = create_app(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    auth_repo = FakeAuthRepo()
    store = FakeSettingsStore()
    with TestClient(app) as test_client:
        app.state.auth_repo = auth_repo
        app.state.settings_store = store
        key = asyncio.run(auth_service.rotate_owner_key(auth_repo))
        assert (
            test_client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield test_client, store


def test_settings_require_auth() -> None:
    app = create_app(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings").status_code == 401
        assert anon.put("/api/settings", json={"image_analysis_mode": "ocr"}).status_code == 401


def test_get_settings_defaults_to_full_analysis(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, _ = client
    # No row yet: the default is full analysis [decided].
    assert c.get("/api/settings").json() == {"image_analysis_mode": "full"}


def test_put_settings_round_trips_the_mode(client: tuple[TestClient, FakeSettingsStore]) -> None:
    c, store = client
    resp = c.put("/api/settings", json={"image_analysis_mode": "ocr"})
    assert resp.status_code == 200
    assert resp.json() == {"image_analysis_mode": "ocr"}
    assert store.values["image_analysis_mode"] == "ocr"
    assert c.get("/api/settings").json() == {"image_analysis_mode": "ocr"}

    assert c.put("/api/settings", json={"image_analysis_mode": "full"}).json() == {
        "image_analysis_mode": "full"
    }


def test_put_settings_rejects_unknown_keys_and_values(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    assert c.put("/api/settings", json={"image_analysis_mode": "everything"}).status_code == 422
    assert c.put("/api/settings", json={"theme": "dark"}).status_code == 422
    assert store.values == {}  # nothing leaked into the store


def test_put_settings_with_empty_patch_changes_nothing(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    store.values["image_analysis_mode"] = "ocr"
    assert c.put("/api/settings", json={}).json() == {"image_analysis_mode": "ocr"}
