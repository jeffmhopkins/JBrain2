"""jmolt's morning-digest tick against real Postgres (M14/M15): the owner-local morning
window, the durable once-a-morning dedup, the registered gate, and a real build from the
action ledger + staged outbox. The notify bus is captured."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmolt_digest import JmoltDigest
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_outbox import ActionLedgerRepo, OutboxRepo
from jbrain.notify import Notification, NotifyBus
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeSettingsStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _jmolt(pid: str) -> SessionContext:
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


def _admin(pid: str) -> SessionContext:
    return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, _admin("")) as s:
        await s.execute(text("DELETE FROM app.jmolt_outbox"))
        await s.execute(text("DELETE FROM app.jmolt_action_ledger"))
    yield


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return str(pid)


class _CaptureBus(NotifyBus):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Notification] = []

    def publish(self, note: Notification) -> None:
        self.sent.append(note)


def _registered_store() -> FakeSettingsStore:
    store = FakeSettingsStore()
    store.values["moltbook_api_key"] = "moltbook_key123456"  # registered
    return store


_MORNING = datetime(2026, 8, 24, 8, 5, tzinfo=UTC)  # inside the owner-local (UTC) window
_MIDDAY = datetime(2026, 8, 24, 13, 0, tzinfo=UTC)  # outside the window


async def _seed(maker, pid: str) -> None:
    async with scoped_session(maker, _jmolt(pid)) as s:
        await ActionLedgerRepo().record(s, pid, action="publish_comment", target="p1")
        await OutboxRepo().stage(s, pid, kind="post", payload={"title": "tide pools tonight"})


async def test_digest_fires_once_in_the_morning_and_enumerates(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    await _seed(maker, pid)
    store = _registered_store()
    bus = _CaptureBus()
    digest = JmoltDigest(maker=maker, settings_store=store, notify=bus)  # type: ignore[arg-type]

    assert await digest.tick(now=_MORNING) is True
    assert len(bus.sent) == 1
    body = bus.sent[0].body
    assert "publish_comment → p1" in body
    assert "post [queued]: tide pools tonight" in body
    assert store.values["moltbook_last_digest"] == "2026-08-24"

    # Dedup: a second morning tick the same day sends nothing more.
    assert await digest.tick(now=_MORNING) is False
    assert len(bus.sent) == 1


async def test_digest_skips_outside_the_window(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    await _seed(maker, pid)
    store = _registered_store()
    bus = _CaptureBus()
    assert (
        await JmoltDigest(maker=maker, settings_store=store, notify=bus).tick(now=_MIDDAY) is False  # type: ignore[arg-type]
    )
    assert bus.sent == []


async def test_digest_skips_when_unregistered(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    await _seed(maker, pid)
    store = FakeSettingsStore()  # no api key
    bus = _CaptureBus()
    assert (
        await JmoltDigest(maker=maker, settings_store=store, notify=bus).tick(now=_MORNING) is False  # type: ignore[arg-type]
    )
    assert bus.sent == []
