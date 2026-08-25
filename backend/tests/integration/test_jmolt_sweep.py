"""jmolt's drip sweep against real Postgres: release-gating, publishing, the tool-free
challenge solve, and the failure-streak guard. Moltbook HTTP and the LLM are faked."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmolt_sweep import JmoltSweep
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_outbox import OutboxRepo
from jbrain.settings_store import MOLTBOOK_FAIL_STREAK_LIMIT
from jbrain.web.moltbook import MoltbookClient
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeSettingsStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@dataclass
class _Result:
    text: str
    parsed: object = None
    reasoning: str = ""


class _FakeRouter:
    def __init__(self, reply: str = "15.00") -> None:
        self.reply = reply

    async def complete(self, task: str, *, system: str, user_text: str, max_tokens: int) -> _Result:
        return _Result(text=self.reply)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return str(pid)


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


async def _key() -> tuple[str, str]:
    return "moltbook_key123456", "jmolt"


def _sweep(maker, store: FakeSettingsStore, handler, *, solver_reply: str = "15.00") -> JmoltSweep:
    client = MoltbookClient(_key, transport=httpx.MockTransport(handler))
    return JmoltSweep(
        maker=maker,
        client=client,
        router=_FakeRouter(solver_reply),  # type: ignore[arg-type]
        settings_store=store,
    )


async def _stage_comment(maker, pid: str) -> str:
    async with scoped_session(maker, _jmolt(pid)) as s:
        return await OutboxRepo().stage(
            s, pid, kind="comment", payload={"post_id": "p1", "content": "hi"}
        )


async def test_queued_rows_are_not_published_until_released(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    await _stage_comment(maker, pid)
    store = FakeSettingsStore()  # switch OFF by default

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"comment": {"id": "c1"}})

    published = await _sweep(maker, store, handler).tick()
    assert published == 0  # queued, switch off → nothing goes out


async def test_owner_release_then_publish(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    row_id = await _stage_comment(maker, pid)
    store = FakeSettingsStore()
    # Owner releases it in the PWA.
    async with scoped_session(maker, _admin(pid)) as s:
        await OutboxRepo().set_status(s, row_id, "released")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"comment": {"id": "c1"}})

    assert await _sweep(maker, store, handler).tick() == 1
    async with scoped_session(maker, _jmolt(pid)) as s:
        rows = await OutboxRepo().list_by_status(s, pid, ("published",))
    assert rows[0].moltbook_id == "c1"


async def test_switch_on_auto_releases_and_publishes(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    await _stage_comment(maker, pid)
    store = FakeSettingsStore()
    store.values["moltbook_autonomy"] = True  # switch ON

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"comment": {"id": "c1"}})

    assert await _sweep(maker, store, handler).tick() == 1


async def test_kill_stops_everything(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    row_id = await _stage_comment(maker, pid)
    async with scoped_session(maker, _admin(pid)) as s:
        await OutboxRepo().set_status(s, row_id, "released")
    store = FakeSettingsStore()
    store.values["moltbook_killed"] = True  # M6

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"comment": {"id": "c1"}})

    assert await _sweep(maker, store, handler).tick() == 0


async def test_verification_challenge_is_solved_and_published(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    row_id = await _stage_comment(maker, pid)
    async with scoped_session(maker, _admin(pid)) as s:
        await OutboxRepo().set_status(s, row_id, "released")
    store = FakeSettingsStore()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/verify"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(
            200,
            json={
                "comment": {"id": "c1"},
                "verification": {"verification_code": "v1", "challenge_text": "10 + 5"},
            },
        )

    assert await _sweep(maker, store, handler).tick() == 1


async def test_streak_stops_writes_after_repeated_verify_failures(
    maker: async_sessionmaker,
) -> None:
    pid = await _owner_pid(maker)
    store = FakeSettingsStore()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/verify"):
            return httpx.Response(200, json={"success": False, "error": "Incorrect"})
        return httpx.Response(
            200,
            json={
                "comment": {"id": "c1"},
                "verification": {"verification_code": "v1", "challenge_text": "bad"},
            },
        )

    sweep = _sweep(maker, store, handler)
    # Each released comment fails verification → bumps the streak.
    for _ in range(MOLTBOOK_FAIL_STREAK_LIMIT):
        rid = await _stage_comment(maker, pid)
        async with scoped_session(maker, _admin(pid)) as s:
            await OutboxRepo().set_status(s, rid, "released")
        await sweep.tick()
    assert store.values["moltbook_verify_fail_streak"] >= MOLTBOOK_FAIL_STREAK_LIMIT

    # Now the guard stops all writes, even a fresh released row.
    rid = await _stage_comment(maker, pid)
    async with scoped_session(maker, _admin(pid)) as s:
        await OutboxRepo().set_status(s, rid, "released")
    assert await sweep.tick() == 0


async def test_unsolvable_challenge_skips_verify_without_spending_the_streak(
    maker: async_sessionmaker,
) -> None:
    # M5: a non-numeric solve is a SKIP, not a submission — the row fails but the streak
    # must NOT advance (else an attacker's unsolvable-challenge flood self-DoSes writes).
    pid = await _owner_pid(maker)
    store = FakeSettingsStore()

    def handler(req: httpx.Request) -> httpx.Response:
        # The write always returns a challenge; /verify would only be hit on a numeric solve.
        assert not req.url.path.endswith("/verify"), "must not submit /verify on a skip"
        return httpx.Response(
            200,
            json={
                "comment": {"id": "c1"},
                "verification": {"verification_code": "v1", "challenge_text": "unsolvable"},
            },
        )

    sweep = _sweep(maker, store, handler, solver_reply="I cannot solve this")
    for _ in range(4):
        rid = await _stage_comment(maker, pid)
        async with scoped_session(maker, _admin(pid)) as s:
            await OutboxRepo().set_status(s, rid, "released")
        await sweep.tick()
    assert store.values.get("moltbook_verify_fail_streak", 0) == 0  # never spent
