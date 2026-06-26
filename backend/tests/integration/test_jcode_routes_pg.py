"""The jcode proxy routes end to end against real Postgres, with a fake control
server. Exercises create → list → get → turn(SSE) → reset → delete under the owner,
so the session index stays honest and the detached turn streams to completion.
"""

import asyncio
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
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.jcode import FakeJcodeClient
from jbrain.settings_store import SqlSettingsStore
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
    app.state.settings = Settings(secure_cookies=False)
    app.state.settings_store = SqlSettingsStore(maker)
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

        # Launcher session management (mirrors the agent-sessions manager): rename,
        # archive, unarchive — owner-only metadata that never touches the control server.
        assert (
            await client.patch(f"/api/jcode/sessions/{sid}", json={"title": "todo spike"})
        ).status_code == 204
        assert (await client.get(f"/api/jcode/sessions/{sid}")).json()["title"] == "todo spike"

        assert (await client.post(f"/api/jcode/sessions/{sid}/archive")).status_code == 204
        assert (await client.get(f"/api/jcode/sessions/{sid}")).json()["archived"] is True
        assert (await client.post(f"/api/jcode/sessions/{sid}/unarchive")).status_code == 204
        assert (await client.get(f"/api/jcode/sessions/{sid}")).json()["archived"] is False

        assert (await client.post(f"/api/jcode/sessions/{sid}/reset")).status_code == 200
        assert (await client.delete(f"/api/jcode/sessions/{sid}")).status_code == 204
        assert (await client.get(f"/api/jcode/sessions/{sid}")).status_code == 404


class _FakeGateway:
    """Records load/unload + reports residency for the warm + status tests."""

    def __init__(self, resident: set[str] | None = None) -> None:
        self.resident: set[str] = set(resident or ())
        self.loaded: list[str] = []
        self.unloaded: list[str] = []

    async def running(self) -> set[str]:
        return set(self.resident)

    async def load(self, served_model: str) -> None:
        self.loaded.append(served_model)
        self.resident = {served_model}

    async def unload(self, served_model: str) -> None:
        self.unloaded.append(served_model)
        self.resident.discard(served_model)


async def test_create_does_not_warm_and_warm_endpoint_swaps(
    maker: async_sessionmaker,
) -> None:
    # Opening a session must NOT touch the gateway — code mode never silently evicts a
    # resident model. Status reports the coder absent with the other model resident; the
    # explicit warm endpoint (owner-confirmed) then evicts it and loads the coder.
    owner_id = await _owner_id(maker)
    app = _app(maker, owner_id)
    app.state.settings = Settings(secure_cookies=False, local_llm_enabled=True)
    gw = _FakeGateway(resident={"gpt-oss-120b"})
    app.state.local_gateway = gw
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.post("/api/jcode/sessions", json={"repo": "r"})).status_code == 201
        await asyncio.sleep(0.05)  # nothing should have run in the background
        assert gw.unloaded == [] and gw.loaded == []  # no surprise swap on create

        status = (await client.get("/api/jcode/model")).json()
        assert status["model"] == "qwen3-coder-next"
        assert status["loaded"] is False
        assert status["resident"] == ["gpt-oss-120b"]  # names what a swap would evict

        assert (await client.post("/api/jcode/model/warm")).status_code == 200
        await asyncio.sleep(0.1)  # let the background warm task run
        assert gw.unloaded == ["gpt-oss-120b"]  # the other model was evicted
        assert gw.loaded == ["qwen3-coder-next"]  # the coder was warmed

        done = (await client.get("/api/jcode/model")).json()
        assert done["loaded"] is True
        assert done["hosting"] is True
        # The warm task has finished, so the bar's signal is back down.
        assert done["warming"] is False


class _BlockingGateway(_FakeGateway):
    """Lists the model resident the moment load() is requested (the gateway's real
    behavior), then blocks until released — so a poll mid-load sees loaded AND warming."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def load(self, served_model: str) -> None:
        self.resident = {served_model}  # resident-as-requested: `loaded` races true here
        self.loaded.append(served_model)
        await self.gate.wait()


async def test_status_reports_warming_while_the_load_is_in_flight(
    maker: async_sessionmaker,
) -> None:
    # The race the bar must survive: the gateway reports the model resident (loaded:true)
    # while the warm task is still loading its weights. `warming` stays true until the
    # task finishes, so the bar keys off it and doesn't vanish mid-load.
    owner_id = await _owner_id(maker)
    app = _app(maker, owner_id)
    app.state.settings = Settings(secure_cookies=False, local_llm_enabled=True)
    gw = _BlockingGateway()
    app.state.local_gateway = gw
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.post("/api/jcode/model/warm")).status_code == 200
        await asyncio.sleep(0.05)  # let the warm task reach the blocked load()

        mid = (await client.get("/api/jcode/model")).json()
        assert mid["loaded"] is True  # the gateway already lists it...
        assert mid["warming"] is True  # ...but the warm task is still loading

        gw.gate.set()  # release the load
        await asyncio.sleep(0.05)  # let the warm task finish + the done-callback fire
        done = (await client.get("/api/jcode/model")).json()
        assert done["warming"] is False


class _PerCallBlockingGateway(_FakeGateway):
    """Each load() blocks on its OWN gate, so overlapping warms can be released one at a
    time — exercising the warming refcount (the flag must hold until the LAST finishes)."""

    def __init__(self) -> None:
        super().__init__()
        self.gates: list[asyncio.Event] = []

    async def load(self, served_model: str) -> None:
        self.resident = {served_model}
        self.loaded.append(served_model)
        gate = asyncio.Event()
        self.gates.append(gate)
        await gate.wait()


async def test_warming_holds_until_the_last_overlapping_warm_finishes(
    maker: async_sessionmaker,
) -> None:
    # Two warm requests back-to-back both warm the same coder. `warming` must stay true
    # until BOTH warm tasks finish — a set keyed on the model name would let the first
    # completion clear it while the second is still loading (the bar would vanish early).
    owner_id = await _owner_id(maker)
    app = _app(maker, owner_id)
    app.state.settings = Settings(secure_cookies=False, local_llm_enabled=True)
    gw = _PerCallBlockingGateway()
    app.state.local_gateway = gw
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.post("/api/jcode/model/warm")).status_code == 200
        assert (await client.post("/api/jcode/model/warm")).status_code == 200
        await asyncio.sleep(0.05)  # both warm tasks reach their blocked load()
        assert len(gw.gates) == 2
        assert (await client.get("/api/jcode/model")).json()["warming"] is True

        gw.gates[0].set()  # first warm finishes...
        await asyncio.sleep(0.05)
        assert (await client.get("/api/jcode/model")).json()["warming"] is True  # ...flag holds

        gw.gates[1].set()  # last warm finishes
        await asyncio.sleep(0.05)
        assert (await client.get("/api/jcode/model")).json()["warming"] is False


async def test_create_forwards_the_selected_model(maker: async_sessionmaker) -> None:
    # No stored selection → the config default reaches the control server; after the
    # owner picks a model (settings store), the next create forwards THAT id.
    owner_id = await _owner_id(maker)
    app = _app(maker, owner_id)
    fake: FakeJcodeClient = app.state.jcode_client
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://t") as client:
        await client.post("/api/jcode/sessions", json={"repo": "r"})
        assert fake.created_models == ["qwen3-coder-next"]  # the config default

        ctx = SessionContext(principal_id=owner_id, principal_kind="owner")
        await SqlSettingsStore(maker).set_jcode_model(ctx, "gpt-oss-120b")
        await client.post("/api/jcode/sessions", json={"repo": "r2"})
        assert fake.created_models[-1] == "gpt-oss-120b"
