"""The entity profile-image surface (PUT/GET /api/entities/{id}/image) with a stubbed repo + a
real FS blob store: owner auth required, non-images rejected, oversize rejected, the bytes round-
trip with a sniffed content-type, and an out-of-scope/unknown entity is a 404."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api.deps import current_principal
from jbrain.auth import service as auth_service
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.storage import FsBlobStore
from tests.unit.fakes import FakeAuthRepo

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class StubAnalysisRepo:
    """Just the two image methods the endpoints touch; `known` is the in-scope entity set."""

    def __init__(self) -> None:
        self.images: dict[str, str] = {}
        self.known = {"e1"}

    async def set_entity_image(self, ctx: Any, entity_id: str, image_sha: str) -> bool:
        if entity_id not in self.known:
            return False
        self.images[entity_id] = image_sha
        return True

    async def entity_image_sha(self, ctx: Any, entity_id: str) -> str | None:
        return self.images.get(entity_id)


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, StubAnalysisRepo, FsBlobStore]]:
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        blob_dir=str(tmp_path),
    )
    app = create_app(settings)
    repo = StubAnalysisRepo()
    blobs = FsBlobStore(tmp_path)
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        app.state.analysis_repo = repo
        app.state.blob_store = blobs
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        assert (
            client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield client, repo, blobs


def _put(client: TestClient, eid: str, data: bytes) -> Any:
    return client.put(f"/api/entities/{eid}/image", files={"file": ("p.png", data, "image/png")})


def test_image_requires_auth(tmp_path: Path) -> None:
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        blob_dir=str(tmp_path),
    )
    app = create_app(settings)
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/entities/e1/image").status_code == 401
        assert _put(anon, "e1", PNG).status_code == 401


def test_upload_then_serve_round_trips_with_sniffed_type(
    api: tuple[TestClient, StubAnalysisRepo, FsBlobStore],
) -> None:
    client, repo, _ = api
    out = _put(client, "e1", PNG)
    assert out.status_code == 200
    assert out.json()["media_type"] == "image/png"
    assert repo.images["e1"] == out.json()["image_sha"]

    served = client.get("/api/entities/e1/image")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.content == PNG


def test_non_image_bytes_are_rejected(
    api: tuple[TestClient, StubAnalysisRepo, FsBlobStore],
) -> None:
    # A lying Content-Type can't sneak a non-image past the magic-byte sniff.
    out = client_put_raw(api[0], "e1", b"this is not an image")
    assert out.status_code == 415


def test_oversize_is_rejected(
    api: tuple[TestClient, StubAnalysisRepo, FsBlobStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jbrain.api.analysis.MAX_IMAGE_BYTES", 8)
    assert _put(api[0], "e1", PNG).status_code == 413


def test_unknown_entity_is_404(api: tuple[TestClient, StubAnalysisRepo, FsBlobStore]) -> None:
    client, _, _ = api
    assert _put(client, "ghost", PNG).status_code == 404  # out of scope / unknown
    assert client.get("/api/entities/ghost/image").status_code == 404  # no image set


def test_upload_is_owner_only(api: tuple[TestClient, StubAnalysisRepo, FsBlobStore]) -> None:
    # A non-owner (capability) principal is refused at the OwnerDep gate before any write.
    client, repo, _ = api
    app = cast(FastAPI, client.app)
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id="cap-1", kind="capability_token", label="agent"
    )
    try:
        assert _put(client, "e1", PNG).status_code == 403
        assert "e1" not in repo.images  # the gate fired before the store/repo
    finally:
        app.dependency_overrides.pop(current_principal, None)


def test_serve_404s_when_the_blob_is_missing(
    api: tuple[TestClient, StubAnalysisRepo, FsBlobStore],
) -> None:
    # The row points at a sha whose bytes are gone (e.g. partial restore) → a clean 404, not 500.
    client, repo, _ = api
    repo.images["e1"] = "0" * 64
    assert client.get("/api/entities/e1/image").status_code == 404


def client_put_raw(client: TestClient, eid: str, data: bytes) -> Any:
    return client.put(f"/api/entities/{eid}/image", files={"file": ("x.bin", data, "image/png")})
