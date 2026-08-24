"""jmolt's nightly run against real Postgres (docs/plans/JMOLT_PLAN.md, W1).

The turn executor is faked (no LLM); the point is the run plumbing and the scheduler
guards. Verifies: a run creates a jmolt-persona session + records the run; the tick
SKIPS when the global kill is on, when jmolt is unregistered, and outside the window;
and fires once inside the window (and not twice the same night).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmolt_night import (
    JmoltNightRunner,
    SingleFlightLane,
    jmolt_night_tick,
)
from jbrain.agent.loop import AgentResult
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.tasks.runner import ExecutedTurn
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeSettingsStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class _FakeExecutor:
    """A stand-in TurnExecutor: returns a fixed result, records the calls it saw."""

    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, **kwargs: object) -> ExecutedTurn:
        self.calls += 1
        return ExecutedTurn(
            result=AgentResult(
                text="I looked around; general is loud, a few submolts are quiet.",
                stop_reason="end_turn",
                steps=3,
                cost_tokens=42,
            ),
            tools=[],
            reasoning="",
        )


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


def _runner(maker: async_sessionmaker, store: FakeSettingsStore, executor: _FakeExecutor):
    return JmoltNightRunner(
        sessions=AgentSessionRepo(maker),
        runlog=AgentRunLog(maker),
        transcript=AgentTranscript(maker),
        executor=executor,  # type: ignore[arg-type]
        settings_store=store,  # type: ignore[arg-type]
    )


async def _jmolt_session_count(maker: async_sessionmaker, owner: SessionContext) -> int:
    async with scoped_session(maker, owner) as session:
        return (
            await session.execute(
                text("SELECT count(*) FROM app.agent_sessions WHERE agent = 'jmolt'")
            )
        ).scalar() or 0


async def test_run_creates_a_jmolt_session_and_records_it(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _FakeExecutor()
    before = await _jmolt_session_count(maker, owner)
    session_id = await _runner(maker, store, executor).run(owner)

    assert executor.calls == 1
    assert await _jmolt_session_count(maker, owner) == before + 1
    # The run is recorded (kind='agent') against that session.
    async with scoped_session(maker, owner) as session:
        status = (
            await session.execute(
                text("SELECT status FROM app.runs WHERE session_id = :sid AND kind = 'agent'"),
                {"sid": session_id},
            )
        ).scalar()
    assert status == "done"


async def test_tick_skips_when_killed(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_api_key"] = "moltbook_key123456"  # registered
    store.values["moltbook_killed"] = True  # but killed (M6)
    lane = SingleFlightLane()
    at_3am = datetime(2026, 8, 25, 3, 5, tzinfo=UTC)
    before = await _jmolt_session_count(maker, owner)
    fired = await jmolt_night_tick(
        maker, _runner(maker, store, _FakeExecutor()), store, lane, now=at_3am
    )
    assert fired is False
    assert not lane.busy()
    assert await _jmolt_session_count(maker, owner) == before  # no run started


async def test_tick_skips_when_unregistered(maker: async_sessionmaker) -> None:
    await _owner(maker)
    store = FakeSettingsStore()  # no key
    lane = SingleFlightLane()
    at_3am = datetime(2026, 8, 25, 3, 5, tzinfo=UTC)
    fired = await jmolt_night_tick(
        maker, _runner(maker, store, _FakeExecutor()), store, lane, now=at_3am
    )
    assert not lane.busy()
    assert fired is False  # window open but no key → no run


async def test_tick_skips_outside_window(maker: async_sessionmaker) -> None:
    await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_api_key"] = "moltbook_key123456"
    lane = SingleFlightLane()
    at_2pm = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)
    fired = await jmolt_night_tick(
        maker, _runner(maker, store, _FakeExecutor()), store, lane, now=at_2pm
    )
    assert fired is False
    assert not lane.busy()


async def test_tick_fires_once_in_window(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_api_key"] = "moltbook_key123456"
    store.values["owner_timezone"] = "UTC"
    lane = SingleFlightLane()
    executor = _FakeExecutor()
    runner = _runner(maker, store, executor)
    at_3am = datetime(2026, 8, 25, 3, 5, tzinfo=UTC)
    before = await _jmolt_session_count(maker, owner)

    fired = await jmolt_night_tick(maker, runner, store, lane, now=at_3am)
    assert fired is True
    await lane.join()  # let the launched run settle
    assert executor.calls == 1
    assert await _jmolt_session_count(maker, owner) == before + 1

    # A second tick the same night must NOT fire again — even on a FRESH lane (simulating
    # a process restart inside the window): the durable last-night date is the guard.
    fresh_lane = SingleFlightLane()
    at_307am = datetime(2026, 8, 25, 3, 7, tzinfo=UTC)
    again = await jmolt_night_tick(maker, runner, store, fresh_lane, now=at_307am)
    await fresh_lane.join()
    assert again is False
    assert executor.calls == 1
    assert await _jmolt_session_count(maker, owner) == before + 1
