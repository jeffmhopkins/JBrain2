"""The jcode proxy routes end to end against real Postgres, with a fake control
server. Exercises create → list → get → stop → restart → reset → delete under the
owner, so the session index stays honest across the terminal-driven lifecycle.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain import box_events
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


@pytest.fixture
async def wired(maker: async_sessionmaker) -> AsyncIterator[async_sessionmaker]:
    """The `box_events` writer wired the way a real process wires it, and unwired
    afterwards — it is module-global, so a leak would have the rest of the suite writing
    to a disposed engine. Only the status test needs it: that route reads the load's
    percentage back off the row the load itself opens.

    The table is emptied first because the container is shared: `in_flight` is a box-wide
    read, so a row another suite left open could shadow this load's."""
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        await session.execute(text("DELETE FROM app.box_events"))
    box_events.configure(maker, source="api")
    yield maker
    box_events.reset()


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

        # Stop pauses the session (kills processes, keeps the checkout); the index
        # mirrors the new status. Restart resumes it.
        assert (await client.post(f"/api/jcode/sessions/{sid}/stop")).status_code == 200
        assert (await client.get(f"/api/jcode/sessions/{sid}")).json()["status"] == "stopped"
        assert (await client.post(f"/api/jcode/sessions/{sid}/restart")).status_code == 200
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


async def test_list_reconciles_a_shell_exit_pause_into_the_mirror(
    maker: async_sessionmaker,
) -> None:
    # A shell exit (Ctrl-D / `exit`) pauses a session on the control server WITHOUT
    # going through the /stop route, so the durable mirror keeps `ready` and the
    # launcher dot would show a paused session as live. Listing reconciles the mirror
    # against the control server's live status, so the dot reflects reality — and the
    # change is persisted (a later get reads `stopped` too).
    owner_id = await _owner_id(maker)
    app = _app(maker, owner_id)
    control = app.state.jcode_client  # the fake control server backing this app
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://t") as client:
        sid = (await client.post("/api/jcode/sessions", json={"repo": "r"})).json()["id"]

        # Simulate the shell-exit pause: flip the control server's status straight,
        # bypassing the /stop route the mirror listens to.
        await control.stop(sid)
        assert (await client.get(f"/api/jcode/sessions/{sid}")).json()["status"] == "ready"

        listed = (await client.get("/api/jcode/sessions")).json()
        assert [(s["id"], s["status"]) for s in listed] == [(sid, "stopped")]
        # Persisted: a direct get now reads the reconciled status too.
        assert (await client.get(f"/api/jcode/sessions/{sid}")).json()["status"] == "stopped"


async def test_list_falls_back_to_the_mirror_when_the_control_server_is_unreachable(
    maker: async_sessionmaker,
) -> None:
    # The durable mirror is the launcher's offline answer: if the control server can't
    # be reached for the live status, listing still returns the last-known rows.
    owner_id = await _owner_id(maker)
    app = _app(maker, owner_id)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://t") as client:
        sid = (await client.post("/api/jcode/sessions", json={"repo": "r"})).json()["id"]

        async def _boom() -> list[dict[str, object]]:
            from jbrain.jcode import JcodeError

            raise JcodeError("control server down")

        app.state.jcode_client.list_sessions = _boom  # type: ignore[method-assign]
        listed = (await client.get("/api/jcode/sessions")).json()
        assert [s["id"] for s in listed] == [sid]


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
        # A settle-wait, not a race: the assertion below is that NOTHING happened, and
        # there is no state to poll for. See `_until` for the ones that were races.
        await asyncio.sleep(0.05)  # nothing should have run in the background
        assert gw.unloaded == [] and gw.loaded == []  # no surprise swap on create

        status = (await client.get("/api/jcode/model")).json()
        assert status["model"] == "qwen3-coder-next"
        assert status["loaded"] is False
        assert status["resident"] == ["gpt-oss-120b"]  # names what a swap would evict

        assert (await client.post("/api/jcode/model/warm")).status_code == 200
        # Both halves matter. `warming is False` alone would pass at t=0, before the task
        # has even started; `gw.loaded` alone would pass while the done-callback is still
        # pending. Together they mean the task ran AND settled.
        await _until(
            lambda: _both(gw.loaded == ["qwen3-coder-next"], _model_is(client, "warming", False)),
            what="the background warm task to run and settle",
        )
        assert gw.unloaded == ["gpt-oss-120b"]  # the other model was evicted
        assert gw.loaded == ["qwen3-coder-next"]  # the coder was warmed

        done = (await client.get("/api/jcode/model")).json()
        assert done["loaded"] is True
        assert done["hosting"] is True
        # The warm task has finished, so the bar's signal is back down.
        assert done["warming"] is False


async def _model_is(client: AsyncClient, field: str, value: object) -> bool:
    """Read one field off GET /api/jcode/model — the predicate `_until` polls."""
    return (await client.get("/api/jcode/model")).json()[field] == value


async def _both(ready: bool, awaitable) -> bool:
    """Combine an already-evaluated condition with an async one, without leaving the
    awaitable un-awaited when the first is False (which would warn and leak)."""
    got = await awaitable
    return ready and got


async def _until(check, *, what: str, timeout_s: float = 5.0) -> None:
    """Wait until an async predicate holds, instead of sleeping a fixed slice.

    These tests drive real asyncio background work — the warm task and the done-callback
    that lowers `warming` — and used fixed `asyncio.sleep(0.05)` waits to let it happen.
    That is a race, not a wait: it passes on an idle machine and loses whenever the runner
    is busy, which is exactly how `test_status_reports_warming_while_the_load_is_in_flight`
    failed in CI while every assertion in it was correct.

    Polling the state the assertion is about is deterministic when the code works, returns
    as soon as it does (so it is FASTER than the sleep in the common case), and still fails
    on a real regression — with a message naming what never happened, rather than a bare
    `assert True is False`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        if await check():
            return
        if loop.time() >= deadline:
            raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")
        await asyncio.sleep(0.01)


class _BlockingGateway(_FakeGateway):
    """Lists the model resident the moment load() is requested (the gateway's real
    behavior), then blocks until released — so a poll mid-load sees loaded AND warming.

    Narrates the load into `box_events` while it blocks, because that open row IS where
    the bar's percentage comes from: the real client opens the span in `load` and the
    watchdog publishes its device samples onto it. `progress` left None models the box
    with no device probe, which publishes nothing and leaves the bar on its estimate."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.progress: float | None = None

    async def load(self, served_model: str) -> None:
        self.resident = {served_model}  # resident-as-requested: `loaded` races true here
        self.loaded.append(served_model)
        async with box_events.span(box_events.MODEL_LOAD, served_model):
            if self.progress is not None:
                await box_events.progress(self.progress)
            await self.gate.wait()


async def test_status_reports_warming_while_the_load_is_in_flight(
    wired: async_sessionmaker,
) -> None:
    # The race the bar must survive: the gateway reports the model resident (loaded:true)
    # while the warm task is still loading its weights. `warming` stays true until the
    # task finishes, so the bar keys off it and doesn't vanish mid-load.
    owner_id = await _owner_id(wired)
    app = _app(wired, owner_id)
    app.state.settings = Settings(secure_cookies=False, local_llm_enabled=True)
    gw = _BlockingGateway()
    gw.progress = 0.5  # the watchdog publishes "half done" onto the open row
    app.state.local_gateway = gw
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.post("/api/jcode/model/warm")).status_code == 200
        # Poll the state the assertions below are about, per _until's own docstring.
        # Waiting on `warming` looks equivalent and is not: it goes true when the task
        # starts, which is BEFORE load() opens its span and publishes the sample, so a
        # busy runner could satisfy this poll and then read progress:None a line later.
        await _until(
            lambda: _model_is(client, "progress", 0.5),
            what="the watchdog's progress sample to land on the open load row",
        )

        mid = (await client.get("/api/jcode/model")).json()
        assert mid["loaded"] is True  # the gateway already lists it...
        assert mid["warming"] is True  # ...but the warm task is still loading
        assert mid["progress"] == 0.5  # ...and the measured fraction is read off its row

        gw.gate.set()  # release the load
        await _until(
            lambda: _model_is(client, "warming", False),
            what="the warm task to finish and its done-callback to fire",
        )
        done = (await client.get("/api/jcode/model")).json()
        assert done["warming"] is False
        assert done["progress"] is None  # settled row → nothing in flight to report


async def test_warm_is_a_noop_when_the_coder_is_already_resident(
    maker: async_sessionmaker,
) -> None:
    # Switching to the coder when it's already on the box must NOT evict anything or
    # re-probe a load (which could force the gateway to re-read the weights) — it's
    # instant. The owner's request to warm an already-resident coder is a no-op.
    owner_id = await _owner_id(maker)
    app = _app(maker, owner_id)
    app.state.settings = Settings(secure_cookies=False, local_llm_enabled=True)
    gw = _FakeGateway(resident={"qwen3-coder-next"})
    app.state.local_gateway = gw
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.post("/api/jcode/model/warm")).status_code == 200
        # Left as a settle-wait deliberately: every assertion below is a NEGATIVE (nothing
        # loaded, nothing evicted), and you cannot poll for "nothing will happen" — a poll
        # would return instantly and prove less than the sleep does.
        await asyncio.sleep(0.1)  # let the (short-circuiting) warm task run
        assert gw.loaded == []  # not reloaded — already resident
        assert gw.unloaded == []  # nothing evicted

        status = (await client.get("/api/jcode/model")).json()
        assert status["loaded"] is True
        # The served context window is reported so the screen can show it ("256k").
        assert status["context_window"] == 262144


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


async def test_create_forwards_the_planner_selection(maker: async_sessionmaker) -> None:
    # The planner half: no stored selection → the config split default (gpt-oss-120b) is
    # forwarded as the planner. Picking "same" collapses to single-model (empty planner);
    # picking a specific installed model forwards its served name.
    owner_id = await _owner_id(maker)
    app = _app(maker, owner_id)
    fake: FakeJcodeClient = app.state.jcode_client
    transport = ASGITransport(app=app)
    ctx = SessionContext(principal_id=owner_id, principal_kind="owner")
    store = SqlSettingsStore(maker)

    async with AsyncClient(transport=transport, base_url="http://t") as client:
        await client.post("/api/jcode/sessions", json={"repo": "r"})
        assert fake.created_planners == ["gpt-oss-120b"]  # the config split default

        # "same" → single-model: no separate planner pin reaches the sandbox.
        await store.set_jcode_planner_model(ctx, "same")
        await client.post("/api/jcode/sessions", json={"repo": "r2"})
        assert fake.created_planners[-1] == ""

        # A specific planner id forwards its served name.
        await store.set_jcode_planner_model(ctx, "gpt-oss-120b")
        await client.post("/api/jcode/sessions", json={"repo": "r3"})
        assert fake.created_planners[-1] == "gpt-oss-120b"
