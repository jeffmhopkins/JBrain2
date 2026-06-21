"""The basemap tile proxy endpoint: any authenticated session (owner or member),
PNG on a hit, 404 on a miss / unknown scheme, 401 when anonymous. The scheme path
segment (dark/light) routes to that scheme's own service."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import keys, service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakePrincipal

_PNG = b"\x89PNG\r\n\x1a\n-fake"
_DEVICE_KEY = "jb1-device-key"


class FakeTileService:
    def __init__(self, data: bytes | None) -> None:
        self.data = data
        self.calls: list[tuple[int, int, int]] = []

    async def tile(self, z: int, x: int, y: int) -> bytes | None:
        self.calls.append((z, x, y))
        return self.data


class FakeTileSet:
    """A scheme→service registry mirroring TileSet: a request's scheme path segment
    selects the service; an unknown scheme resolves to None (the endpoint 404s)."""

    def __init__(self, services: dict[str, FakeTileService]) -> None:
        self.services = services

    def service(self, scheme: str | None) -> FakeTileService | None:
        return self.services.get(scheme or "dark")


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def tiles() -> FakeTileService:
    return FakeTileService(_PNG)


@pytest.fixture
def tile_set(tiles: FakeTileService) -> FakeTileSet:
    # `light` serves a distinct payload so a test can prove the scheme routes.
    return FakeTileSet({"dark": tiles, "light": FakeTileService(b"\x89PNG-light")})


@pytest.fixture
def client(repo: FakeAuthRepo, tile_set: FakeTileSet) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.tile_set = tile_set
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def login_member(client: TestClient, repo: FakeAuthRepo) -> None:
    repo.principals.append(
        FakePrincipal(
            id="dev-1",
            kind="device_key",
            key_hash=keys.hash_key(_DEVICE_KEY),
            label="Phone",
            subject_id="subj-1",
        )
    )
    assert client.post("/api/session/mint", json={"device_key": _DEVICE_KEY}).status_code == 204


def test_tiles_require_a_session(client: TestClient) -> None:
    # Anonymous (no cookie) is rejected — no upstream fetch for the unauthenticated.
    assert client.get("/api/tiles/dark/5/1/1.png").status_code == 401


def test_a_member_session_can_load_tiles(
    client: TestClient, repo: FakeAuthRepo, tiles: FakeTileService
) -> None:
    # The JBrain360 app authenticates as a device/member, not the owner — its map
    # must render too (regression: the proxy used to be owner-only).
    login_member(client, repo)
    resp = client.get("/api/tiles/dark/5/1/1.png")
    assert resp.status_code == 200
    assert resp.content == _PNG
    assert tiles.calls == [(5, 1, 1)]


def test_tile_hit_returns_png(
    client: TestClient, repo: FakeAuthRepo, tiles: FakeTileService
) -> None:
    login(client, repo)
    resp = client.get("/api/tiles/dark/5/1/1.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "max-age" in resp.headers["cache-control"]
    assert resp.content == _PNG
    assert tiles.calls == [(5, 1, 1)]


def test_scheme_selects_its_own_service(
    client: TestClient, repo: FakeAuthRepo, tiles: FakeTileService
) -> None:
    # The light scheme routes to the light service (distinct payload), not the dark
    # one — so the app's toggle truly switches basemaps.
    login(client, repo)
    resp = client.get("/api/tiles/light/5/1/1.png")
    assert resp.status_code == 200
    assert resp.content == b"\x89PNG-light"
    assert tiles.calls == []  # the dark service was never touched


def test_unknown_scheme_is_404(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.get("/api/tiles/sepia/5/1/1.png").status_code == 404


def test_tile_miss_is_404(client: TestClient, repo: FakeAuthRepo, tiles: FakeTileService) -> None:
    login(client, repo)
    tiles.data = None  # disabled / out of range / upstream failure
    assert client.get("/api/tiles/dark/5/1/1.png").status_code == 404
