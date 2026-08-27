"""jmolt's drip sweep against real Postgres: release-gating, publishing, the tool-free
challenge solve, and the failure-streak guard. Moltbook HTTP and the LLM are faked."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmolt_sweep import (
    JMOLT_MAX_WRITES_PER_TICK,
    JMOLT_WRITE_GAP_S,
    JmoltSweep,
)
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


def _sweep(
    maker,
    store: FakeSettingsStore,
    handler,
    *,
    solver_reply: str = "15.00",
    slept: list[float] | None = None,
    clock: "_FakeClock | None" = None,
) -> JmoltSweep:
    client = MoltbookClient(_key, transport=httpx.MockTransport(handler))

    async def _sleep(seconds: float) -> None:
        if slept is not None:
            slept.append(seconds)
        if clock is not None:
            clock.advance(seconds)

    return JmoltSweep(
        maker=maker,
        client=client,
        router=_FakeRouter(solver_reply),  # type: ignore[arg-type]
        settings_store=store,  # type: ignore[arg-type]
        sleep=_sleep,
        clock=clock.now if clock is not None else None,
    )


class _FakeClock:
    """A monotonic clock the test drives, so a Retry-After hold can be observed without
    waiting out a real one."""

    def __init__(self) -> None:
        self._t = 1000.0

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


async def _stage_comment(maker, pid: str) -> str:
    async with scoped_session(maker, _jmolt(pid)) as s:
        row_id = await OutboxRepo().stage(
            s, pid, kind="comment", payload={"post_id": "p1", "content": "hi"}
        )
    assert row_id is not None  # no dedup_key here, so a fresh stage always returns an id
    return row_id


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
    assert int(store.values["moltbook_verify_fail_streak"]) >= MOLTBOOK_FAIL_STREAK_LIMIT  # type: ignore[call-overload]

    # Now the guard stops all writes, even a fresh released row.
    rid = await _stage_comment(maker, pid)
    async with scoped_session(maker, _admin(pid)) as s:
        await OutboxRepo().set_status(s, rid, "released")
    assert await sweep.tick() == 0


async def _stage_released_comments(maker, pid: str, n: int) -> None:
    async with scoped_session(maker, _jmolt(pid)) as s:
        for i in range(n):
            await OutboxRepo().stage(
                s, pid, kind="comment", payload={"post_id": f"p{i}", "content": f"reply {i}"}
            )
    async with scoped_session(maker, _admin(pid)) as s:
        for row in await OutboxRepo().list_by_status(s, pid, ("queued",)):
            await OutboxRepo().set_status(s, row.id, "released")


async def test_the_drip_spaces_its_writes(maker: async_sessionmaker) -> None:
    # THE REGRESSION. The tick used to publish every due row back-to-back: measured on the
    # box, writes went out 0.19-0.43s apart, twelve inside three seconds, and the platform
    # 429'd seven of them into terminal failure. RateLedger could not stop it — it counts per
    # MINUTE, so a whole minute's budget spent in one second satisfies it exactly as well.
    pid = await _owner_pid(maker)
    await _stage_released_comments(maker, pid, 4)
    slept: list[float] = []
    sweep = _sweep(
        maker,
        FakeSettingsStore(),
        lambda _r: httpx.Response(200, json={"comment": {"id": "c1"}}),
        slept=slept,
    )

    assert await sweep.tick() == 4
    # One gap BETWEEN each pair of writes — never before the first, so a single-row tick is
    # still immediate.
    assert slept == [JMOLT_WRITE_GAP_S] * 3


async def test_a_tick_publishes_at_most_its_bound(maker: async_sessionmaker) -> None:
    # A long queue drains over several ticks rather than in one flush, so the spacing cannot
    # run a tick into the next one.
    pid = await _owner_pid(maker)
    await _stage_released_comments(maker, pid, JMOLT_MAX_WRITES_PER_TICK + 5)
    sweep = _sweep(
        maker, FakeSettingsStore(), lambda _r: httpx.Response(200, json={"comment": {"id": "c1"}})
    )

    assert await sweep.tick() == JMOLT_MAX_WRITES_PER_TICK
    async with scoped_session(maker, _jmolt(pid)) as s:
        left = await OutboxRepo().list_by_status(s, pid, ("released",))
    assert len(left) == 5  # the tail waits, it is not dropped


async def test_a_retry_after_holds_the_queue_for_what_the_platform_asked(
    maker: async_sessionmaker,
) -> None:
    # The header was parsed onto the exception and read by nobody, so a 429 saying "wait 300s"
    # got knocked on again 60s later. Five more knocks on a door that had just said stop.
    pid = await _owner_pid(maker)
    await _stage_released_comments(maker, pid, 1)
    clock = _FakeClock()
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "300"}, json={"error": "slow down"})
        return httpx.Response(200, json={"comment": {"id": "c1"}})

    sweep = _sweep(maker, FakeSettingsStore(), handler, clock=clock)
    assert await sweep.tick() == 0  # 429 → deferred, and a 300s hold recorded

    clock.advance(120.0)  # two ticks' worth — still inside the hold
    assert await sweep.tick() == 0
    assert calls["n"] == 1  # the platform was NOT knocked on again

    clock.advance(200.0)  # now past it
    assert await sweep.tick() == 1
    assert calls["n"] == 2


async def test_an_absurd_retry_after_cannot_park_the_queue_indefinitely(
    maker: async_sessionmaker,
) -> None:
    # The header is remote-controlled. A hostile or fat-fingered value must not be able to
    # stop jmolt writing for a day.
    pid = await _owner_pid(maker)
    await _stage_released_comments(maker, pid, 1)
    clock = _FakeClock()
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "999999"}, json={})
        return httpx.Response(200, json={"comment": {"id": "c1"}})

    sweep = _sweep(maker, FakeSettingsStore(), handler, clock=clock)
    assert await sweep.tick() == 0
    clock.advance(1000.0)  # past the cap, nowhere near what the header asked for
    assert await sweep.tick() == 1


async def test_rate_limit_defers_the_row_instead_of_failing_it(maker: async_sessionmaker) -> None:
    # A 429 at publish must not DROP the write — last night's busy tail failed terminally this
    # way. The row stays `released` (not `failed`) and a later tick retries it once the platform
    # recovers.
    pid = await _owner_pid(maker)
    rid = await _stage_comment(maker, pid)
    async with scoped_session(maker, _admin(pid)) as s:
        await OutboxRepo().set_status(s, rid, "released")
    store = FakeSettingsStore()

    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"comment": {"id": "c1"}})

    sweep = _sweep(maker, store, handler)
    # First tick: rate-limited → deferred. Nothing published; the row is still released, NOT failed.
    assert await sweep.tick() == 0
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert await OutboxRepo().list_by_status(s, pid, ("failed",)) == []
        assert len(await OutboxRepo().list_by_status(s, pid, ("released",))) == 1
    # Second tick: the platform has recovered → the same row publishes.
    assert await sweep.tick() == 1
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert (await OutboxRepo().list_by_status(s, pid, ("published",)))[0].moltbook_id == "c1"


async def test_rate_limit_stops_the_tick_rather_than_hammering(maker: async_sessionmaker) -> None:
    # On a 429 the tick STOPS instead of trying every remaining row — the anti-burst backoff.
    # Three released comments, the platform throttling: only ONE write is attempted this tick,
    # and all three rows stay released for a later tick.
    pid = await _owner_pid(maker)
    for _ in range(3):
        rid = await _stage_comment(maker, pid)
        async with scoped_session(maker, _admin(pid)) as s:
            await OutboxRepo().set_status(s, rid, "released")
    store = FakeSettingsStore()

    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "rate limited"})

    assert await _sweep(maker, store, handler).tick() == 0
    assert calls["n"] == 1  # stopped after the first 429, did not hammer the other two
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert await OutboxRepo().list_by_status(s, pid, ("failed",)) == []
        assert len(await OutboxRepo().list_by_status(s, pid, ("released",))) == 3


async def test_tick_stamps_the_drip_heartbeat(maker: async_sessionmaker) -> None:
    # Each sweep stamps its heartbeat so the PWA can show "drip last ran …"; the sweep
    # otherwise persists nothing about its 60-second cadence. The owner must exist (the
    # sweep resolves it itself), but the test doesn't need the id.
    await _owner_pid(maker)
    store = FakeSettingsStore()

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    await _sweep(maker, store, handler).tick(now=datetime(2026, 8, 26, 12, 40, tzinfo=UTC))
    assert str(store.values["moltbook_drip_last_swept"]).startswith("2026-08-26T12:40")


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
