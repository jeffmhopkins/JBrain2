"""The JPet HTTP surface end-to-end (docs/plans/JPET_PLAN.md W1) against real Postgres.

Drives the actual FastAPI app: owner login, GET /api/pet (creates + returns the pet),
and POST /api/pet/command (feed + move mutate and return the new state). Owner-gated:
an unauthenticated caller is refused. (The `GET /pet/stream` SSE endpoint is an
*infinite* response, which httpx's ASGITransport buffers rather than streams — so it
can't be consumed through TestClient; its fan-out is covered by the broadcaster unit
tests and the command-sync integration test.)
"""

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

PET_FIELDS = {
    "name",
    "domain",
    "mood",
    "emotion",
    "speech",
    "asleep",
    "pos_x",
    "pos_z",
    "target_x",
    "target_z",
    "facing",
    "action",
    "script",
    "carrying",
    "lights_on",
    "objects",
}


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_pet_api_round_trip(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        # Owner-gated: no session → 401.
        assert client.get("/api/pet").status_code == 401

        login = client.post("/api/auth/session", json={"owner_key": key, "device_label": "it"})
        assert login.status_code == 204

        pet = client.get("/api/pet").json()
        assert set(pet) == PET_FIELDS  # the frozen wire contract for both surfaces
        assert pet["name"] == "Blink"
        assert "ball" in pet["objects"]  # the room is seeded

        # A play button expands to a bounded, terminating script the wall plays out.
        danced = client.post("/api/pet/command", json={"action": "dance"}).json()
        assert danced["script"], "dance should produce a script"
        assert danced["script"][-1]["action"] in {"sit", "idle", "sleep"}  # always terminates

        # A parent raw-move walks the pet to a floor point.
        moved = client.post("/api/pet/command", json={"action": "move", "x": 0.5, "z": -0.3}).json()
        assert moved["action"] == "walk"
        assert moved["target_x"] == pytest.approx(0.5)
        assert moved["target_z"] == pytest.approx(-0.3)

        # An unknown action is rejected by the request schema.
        assert client.post("/api/pet/command", json={"action": "explode"}).status_code == 422

        # The internal, un-authed read the on-box wall display uses (the pet now exists).
        internal = client.get("/internal/pet")
        assert internal.status_code == 200
        assert set(internal.json()) == PET_FIELDS
