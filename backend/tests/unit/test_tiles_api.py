"""The basemap tile proxy endpoint: owner-only, PNG on a hit, 404 on a miss."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

_PNG = b"\x89PNG\r\n\x1a\n-fake"


class FakeTileService:
    def __init__(self, data: bytes | None) -> None:
        self.data = data
        self.calls: list[tuple[int, int, int]] = []

    async def tile(self, z: int, x: int, y: int) -> bytes | None:
        self.calls.append((z, x, y))
        return self.data


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def tiles() -> FakeTileService:
    return FakeTileService(_PNG)


@pytest.fixture
def client(repo: FakeAuthRepo, tiles: FakeTileService) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.tile_service = tiles
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def test_tiles_require_owner(client: TestClient) -> None:
    assert client.get("/api/tiles/5/1/1.png").status_code == 401


def test_tile_hit_returns_png(
    client: TestClient, repo: FakeAuthRepo, tiles: FakeTileService
) -> None:
    login(client, repo)
    resp = client.get("/api/tiles/5/1/1.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "max-age" in resp.headers["cache-control"]
    assert resp.content == _PNG
    assert tiles.calls == [(5, 1, 1)]


def test_tile_miss_is_404(client: TestClient, repo: FakeAuthRepo, tiles: FakeTileService) -> None:
    login(client, repo)
    tiles.data = None  # disabled / out of range / upstream failure
    assert client.get("/api/tiles/5/1/1.png").status_code == 404
