"""The direct render API (Wave L3) without Postgres or ComfyUI: owner gating on every route,
the configuration gate (comfyui_url unset → generate/edit absent/404), and the list shape.
Real-PG insert/list/RLS behavior is covered in tests/integration/test_images_render_pg.py."""

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.models.images import GeneratedImage
from tests.unit.fakes import FakeAuthRepo


def _settings(**kw: Any) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    return Settings(**kw)


@pytest.fixture
def app_anon() -> Iterator[FastAPI]:
    """An app with image hosting ON (so the render routes mount) but no logged-in session —
    used to assert the owner gate fires before anything else."""
    app = create_app(_settings(comfyui_url="http://comfyui:8188"))
    with TestClient(app):
        app.state.auth_repo = FakeAuthRepo()
        yield app


@pytest.fixture
def owner_client() -> Iterator[tuple[TestClient, FastAPI]]:
    app = create_app(_settings(comfyui_url="http://comfyui:8188"))
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        login = client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        assert login.status_code == 204
        yield client, app


# ----- owner gating (security-100% on the new routes) -----


def test_list_requires_auth() -> None:
    app = create_app(_settings())  # hosting off — the list route is still mounted
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/images/generated").status_code == 401


def test_generate_requires_auth(app_anon: FastAPI) -> None:
    with TestClient(app_anon) as anon:
        resp = anon.post("/api/images/generate", json={"prompt": "x"})
        assert resp.status_code == 401


def test_edit_requires_auth(app_anon: FastAPI) -> None:
    with TestClient(app_anon) as anon:
        resp = anon.post(
            "/api/images/edit", data={"spec": json.dumps({"prompt": "x", "sourceImageId": "a"})}
        )
        assert resp.status_code == 401


def test_non_owner_is_forbidden() -> None:
    """The owner gate (OwnerDep) every render route depends on 403s a non-owner principal — the
    same dependency that fronts list/generate/edit. End-to-end RLS owner-scoping is asserted on
    real PG in the integration suite; here we pin the route's gate."""
    from fastapi import HTTPException

    from jbrain.api.deps import owner_only
    from jbrain.auth.service import PrincipalInfo

    with pytest.raises(HTTPException) as exc:
        asyncio.run(owner_only(PrincipalInfo(id="x", kind="device_key", label="phone")))
    assert exc.value.status_code == 403


# ----- the configuration gate: comfyui_url unset → generate/edit absent (404) -----


def test_generate_absent_when_hosting_off() -> None:
    app = create_app(_settings())  # no comfyui_url
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        # The render router was never mounted, so the path 404s even for the owner.
        assert client.post("/api/images/generate", json={"prompt": "x"}).status_code == 404


def test_edit_absent_when_hosting_off() -> None:
    app = create_app(_settings())
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        resp = client.post(
            "/api/images/edit", data={"spec": json.dumps({"prompt": "x", "sourceImageId": "a"})}
        )
        assert resp.status_code == 404


# ----- the list shape (owner, fake repo, no DB) -----


class _FakeRepo:
    def __init__(self, rows: list[GeneratedImage]) -> None:
        self._rows = rows

    async def list(self, session: object, *, limit: int) -> list[GeneratedImage]:
        return self._rows[:limit]


def test_list_returns_summary_shape(
    owner_client: tuple[TestClient, FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = owner_client
    row = GeneratedImage(
        id=uuid.uuid4(),
        blob_sha256="ab" * 32,
        kind="generate",
        model="qwen-image-2512",
        prompt="a red bicycle",
        source_sha256=None,
        width=1024,
        height=1024,
        steps=20,
        seed=4242,
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    app.state.generated_image_repo = _FakeRepo([row])

    # The list route opens scoped_session(maker, ctx); patch it to a noop so no DB is needed.
    import jbrain.api.images_render as mod

    class _Scoped:
        def __init__(self, *_a: object) -> None: ...

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(mod, "scoped_session", _Scoped)

    body = client.get("/api/images/generated").json()
    assert isinstance(body, list) and len(body) == 1
    item = body[0]
    assert item["id"] == str(row.id)
    assert item["kind"] == "generate"
    assert item["prompt"] == "a red bicycle"
    assert (item["width"], item["height"]) == (1024, 1024)
    assert item["model"] == "qwen-image-2512"
    assert item["seed"] == 4242
    assert item["created_at"].startswith("2026-01-02T03:04:05")
