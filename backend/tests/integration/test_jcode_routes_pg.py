"""The jcode proxy routes end to end against real Postgres, with a fake control
server. Exercises create → list → get → turn(SSE) → reset → delete under the owner,
so the session index stays honest and the detached turn streams to completion.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.api import jcode
from jbrain.api.deps import current_principal
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.jcode import FakeJcodeClient
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner_id(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


def _app(maker: async_sessionmaker, owner_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(jcode.router, prefix="/api")
    app.state.session_maker = maker
    app.state.jcode_client = FakeJcodeClient()
    app.state.jcode_turns = {}
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id=owner_id, kind="owner", label="owner"
    )
    return app


async def test_full_session_lifecycle_through_the_routes(maker: async_sessionmaker) -> None:
    owner_id = await _owner_id(maker)
    app = _app(maker, owner_id)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://t") as client:
        created = await client.post("/api/jcode/sessions", json={"repo": "github.com/me/r"})
        assert created.status_code == 201
        sid = created.json()["id"]

        # The index persisted the row (owner-scoped read through the route).
        listed = await client.get("/api/jcode/sessions")
        assert [s["id"] for s in listed.json()] == [sid]
        assert (await client.get(f"/api/jcode/sessions/{sid}")).json()["repo"] == "github.com/me/r"

        # The turn streams the fake control server's frames to completion.
        async with client.stream(
            "POST", f"/api/jcode/sessions/{sid}/turn", json={"prompt": "add a button"}
        ) as resp:
            assert resp.status_code == 200
            events = [line async for line in resp.aiter_lines() if line.startswith("data:")]
        assert any('"done"' in e for e in events)

        # After the turn, the index status settled back to ready.
        assert (await client.get(f"/api/jcode/sessions/{sid}")).json()["status"] == "ready"

        assert (await client.post(f"/api/jcode/sessions/{sid}/reset")).status_code == 200
        assert (await client.delete(f"/api/jcode/sessions/{sid}")).status_code == 204
        assert (await client.get(f"/api/jcode/sessions/{sid}")).status_code == 404
