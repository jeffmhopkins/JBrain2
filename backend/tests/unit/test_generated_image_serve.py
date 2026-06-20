"""The generated-image serving surface (GET /api/images/generated/{id}) with a stubbed repo + a
real FS blob store: owner auth required (401 unauth, 403 non-owner), the bytes round-trip with a
sniffed content-type, and a row the owner can't see — or a row whose blob is gone — is a 404.

The repo + session-maker are stubbed so the handler's owner-gate, RLS-scoped lookup, sniff, and
404 branches run without Postgres (the real RLS firewall is covered by the integration tests)."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
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
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class _FakeSession:
    """A no-op stand-in for an AsyncSession: `begin()` and the GUC `execute()`s are accepted so
    `scoped_session` runs unchanged, but no real DB is touched (the stub repo ignores it)."""

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> "_FakeSession":
        return self

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        return None


class _FakeSessionMaker:
    def __call__(self) -> _FakeSession:
        return _FakeSession()


class StubGeneratedRepo:
    """Just `get`, keyed by id (the session is ignored — the real RLS firewall is integration-
    tested). `rows` maps image_id -> blob_sha256; an unknown id returns None like an out-of-scope
    row would under RLS."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def get(self, session: Any, image_id: str) -> Any | None:
        sha = self.rows.get(image_id)
        return None if sha is None else SimpleNamespace(id=image_id, blob_sha256=sha)


def _make_app(tmp_path: Path) -> tuple[FastAPI, StubGeneratedRepo, FsBlobStore]:
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        blob_dir=str(tmp_path),
    )
    app = create_app(settings)
    return app, StubGeneratedRepo(), FsBlobStore(tmp_path)


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, StubGeneratedRepo, FsBlobStore]]:
    app, repo, blobs = _make_app(tmp_path)
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        app.state.generated_image_repo = repo
        app.state.blob_store = blobs
        app.state.session_maker = _FakeSessionMaker()
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        assert (
            client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield client, repo, blobs


def test_serve_requires_auth(tmp_path: Path) -> None:
    app, _, _ = _make_app(tmp_path)
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/images/generated/img-1").status_code == 401


def test_serve_is_owner_only(api: tuple[TestClient, StubGeneratedRepo, FsBlobStore]) -> None:
    # A non-owner (capability) principal is refused at the OwnerDep gate before any lookup.
    client, repo, blobs = api
    sha = asyncio.run(blobs.put(PNG))
    repo.rows["img-1"] = sha
    app = cast(FastAPI, client.app)
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id="cap-1", kind="capability_token", label="agent"
    )
    try:
        assert client.get("/api/images/generated/img-1").status_code == 403
    finally:
        app.dependency_overrides.pop(current_principal, None)


def test_owner_get_round_trips_with_sniffed_type(
    api: tuple[TestClient, StubGeneratedRepo, FsBlobStore],
) -> None:
    client, repo, blobs = api
    repo.rows["png-1"] = asyncio.run(blobs.put(PNG))
    repo.rows["jpg-1"] = asyncio.run(blobs.put(JPEG))

    png = client.get("/api/images/generated/png-1")
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"
    assert png.content == PNG

    # The media type is sniffed per blob, not a stored content-type.
    jpg = client.get("/api/images/generated/jpg-1")
    assert jpg.status_code == 200
    assert jpg.headers["content-type"] == "image/jpeg"
    assert jpg.content == JPEG


def test_unknown_id_is_404(api: tuple[TestClient, StubGeneratedRepo, FsBlobStore]) -> None:
    # An out-of-scope/missing row is hidden by RLS, so the handler sees None → a clean 404.
    client, _, _ = api
    assert client.get("/api/images/generated/ghost").status_code == 404


def test_missing_blob_is_404(api: tuple[TestClient, StubGeneratedRepo, FsBlobStore]) -> None:
    # The row points at a sha whose bytes are gone (e.g. partial restore) → 404, not 500.
    client, repo, _ = api
    repo.rows["img-1"] = "0" * 64
    assert client.get("/api/images/generated/img-1").status_code == 404
