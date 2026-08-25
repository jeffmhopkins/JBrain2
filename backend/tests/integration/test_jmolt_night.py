"""jmolt's nightly run against real Postgres (docs/plans/JMOLT_PLAN.md, W1).

The turn executor is faked (no LLM); the point is the run plumbing and the scheduler
guards. Verifies: a run creates a jmolt-persona session + records the run; the tick
SKIPS when the global kill is on, when jmolt is unregistered, and outside the window;
and fires once inside the window (and not twice the same night).
"""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import cast

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
from jbrain.llm import UserMessage
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


async def _ready(model: str | None) -> str | None:
    """An already-resolved served-model loader result — the async shape the runner awaits."""
    return model


def _stepped_clock(step_s: float) -> Callable[[], datetime]:
    """A deterministic clock advancing `step_s` per call (the faked executor returns
    instantly, so a real clock would spin). The sittings loop reads it once per iteration:
    with the 3600 s budget and 300 s margin, elapsed crosses at 3300 s, so step 2000 → 1
    sitting, step 600 → 5 sittings."""
    state = {"t": datetime(2026, 8, 25, 3, 0, tzinfo=UTC)}

    def _now() -> datetime:
        cur = state["t"]
        state["t"] = cur + timedelta(seconds=step_s)
        return cur

    return _now


def _runner(
    maker: async_sessionmaker,
    store: FakeSettingsStore,
    executor: _FakeExecutor,
    *,
    clock: Callable[[], datetime] | None = None,
    served_model_loader: Callable[[], object] | None = None,
):
    return JmoltNightRunner(
        sessions=AgentSessionRepo(maker),
        runlog=AgentRunLog(maker),
        transcript=AgentTranscript(maker),
        executor=executor,  # type: ignore[arg-type]
        settings_store=store,  # type: ignore[arg-type]
        maker=maker,
        clock=clock or _stepped_clock(2000),  # default: one sitting per night
        served_model_loader=served_model_loader,  # type: ignore[arg-type]
    )


async def _jmolt_session_count(maker: async_sessionmaker, owner: SessionContext) -> int:
    async with scoped_session(maker, owner) as session:
        return (
            await session.execute(
                text("SELECT count(*) FROM app.agent_sessions WHERE agent = 'jmolt'")
            )
        ).scalar() or 0


async def _run_count(maker: async_sessionmaker, owner: SessionContext, session_id: str) -> int:
    async with scoped_session(maker, owner) as session:
        return (
            await session.execute(
                text("SELECT count(*) FROM app.runs WHERE session_id = :sid AND kind = 'agent'"),
                {"sid": session_id},
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


async def test_run_does_multiple_sittings_under_one_session(maker: async_sessionmaker) -> None:
    # A full night is a SEQUENCE of sittings — one agent_session, a recorded run per sitting.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _FakeExecutor()
    before = await _jmolt_session_count(maker, owner)
    session_id = await _runner(maker, store, executor, clock=_stepped_clock(600)).run(owner)

    assert executor.calls == 5  # ~10-min sittings across the hour (see _stepped_clock)
    assert await _jmolt_session_count(maker, owner) == before + 1  # ONE session for the night
    assert await _run_count(maker, owner, session_id) == 5  # five sitting-runs under it


async def test_run_halts_sittings_on_kill(maker: async_sessionmaker) -> None:
    # M6: a global kill engaged mid-night stops launching further sittings.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_killed"] = True
    executor = _FakeExecutor()
    session_id = await _runner(maker, store, executor, clock=_stepped_clock(600)).run(owner)

    # The clock leaves room for several sittings, but the kill halts the loop after the
    # first (the tick guards sitting one; the loop re-checks the kill before each next one).
    assert executor.calls == 1
    assert await _run_count(maker, owner, session_id) == 1


async def test_tick_skips_when_killed(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_api_key"] = "moltbook_key123456"  # registered
    store.values["moltbook_killed"] = True  # but killed (M6)
    lane = SingleFlightLane()
    at_3am = datetime(2026, 8, 25, 3, 5, tzinfo=UTC)
    before = await _jmolt_session_count(maker, owner)
    fired = await jmolt_night_tick(
        maker,
        _runner(maker, store, _FakeExecutor()),
        store,  # type: ignore[arg-type]
        lane,
        now=at_3am,
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
        maker,
        _runner(maker, store, _FakeExecutor()),
        store,  # type: ignore[arg-type]
        lane,
        now=at_3am,
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
        maker,
        _runner(maker, store, _FakeExecutor()),
        store,  # type: ignore[arg-type]
        lane,
        now=at_2pm,
    )
    assert fired is False
    assert not lane.busy()


async def test_tick_skips_when_nightly_run_disabled(maker: async_sessionmaker) -> None:
    # The owner turned the nightly run off (independent of the global kill).
    await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_api_key"] = "moltbook_key123456"
    store.values["moltbook_night_enabled"] = False
    lane = SingleFlightLane()
    at_3am = datetime(2026, 8, 25, 3, 5, tzinfo=UTC)
    fired = await jmolt_night_tick(
        maker,
        _runner(maker, store, _FakeExecutor()),
        store,  # type: ignore[arg-type]
        lane,
        now=at_3am,
    )
    assert fired is False and not lane.busy()


async def test_tick_uses_the_configured_hour(maker: async_sessionmaker) -> None:
    # The run fires at the owner-chosen hour, not the 03:00 default.
    await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_api_key"] = "moltbook_key123456"
    store.values["moltbook_night_hour"] = 22
    lane = SingleFlightLane()
    runner = _runner(maker, store, _FakeExecutor())
    # 03:00 no longer fires…
    assert (
        await jmolt_night_tick(
            maker,
            runner,
            store,  # type: ignore[arg-type]
            lane,
            now=datetime(2026, 8, 25, 3, 5, tzinfo=UTC),
        )
        is False
    )
    # …but 22:00 does.
    fired = await jmolt_night_tick(
        maker,
        runner,
        store,  # type: ignore[arg-type]
        lane,
        now=datetime(2026, 8, 25, 22, 5, tzinfo=UTC),
    )
    assert fired is True
    await lane.join()


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

    fired = await jmolt_night_tick(maker, runner, store, lane, now=at_3am)  # type: ignore[arg-type]
    assert fired is True
    await lane.join()  # let the launched run settle
    assert executor.calls == 1
    assert await _jmolt_session_count(maker, owner) == before + 1

    # A second tick the same night must NOT fire again — even on a FRESH lane (simulating
    # a process restart inside the window): the durable last-night date is the guard.
    fresh_lane = SingleFlightLane()
    at_307am = datetime(2026, 8, 25, 3, 7, tzinfo=UTC)
    again = await jmolt_night_tick(maker, runner, store, fresh_lane, now=at_307am)  # type: ignore[arg-type]
    await fresh_lane.join()
    assert again is False
    assert executor.calls == 1
    assert await _jmolt_session_count(maker, owner) == before + 1


class _HoldWatchingExecutor(_FakeExecutor):
    """Records the night hold that was set WHILE its turn ran — proves the box was reserved
    for the duration of the sitting, not just at the edges."""

    def __init__(self, store: FakeSettingsStore, owner: SessionContext) -> None:
        super().__init__()
        self._store = store
        self._owner = owner
        self.hold_during_turn: frozenset[str] = frozenset()

    async def run_turn(self, **kwargs: object) -> ExecutedTurn:
        self.hold_during_turn = await self._store.night_hold_names(self._owner)
        return await super().run_turn(**kwargs)


async def test_run_reserves_the_box_for_the_night_then_releases(maker: async_sessionmaker) -> None:
    # The night hold pins the served model for the hour, and is cleared once the night ends.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _HoldWatchingExecutor(store, owner)
    await _runner(maker, store, executor, served_model_loader=lambda: _ready("gpt-oss-120b")).run(
        owner
    )

    assert executor.hold_during_turn == frozenset({"gpt-oss-120b"})  # reserved DURING the sitting
    assert await store.night_hold_names(owner) == frozenset()  # released after the night


async def test_run_releases_the_box_even_when_a_sitting_raises(maker: async_sessionmaker) -> None:
    # The release is in a `finally`: a blowing-up night still frees the box (no stuck reservation).
    owner = await _owner(maker)
    store = FakeSettingsStore()

    class _Boom(_FakeExecutor):
        async def run_turn(self, **kwargs: object) -> ExecutedTurn:
            raise RuntimeError("sitting exploded")

    await _runner(maker, store, _Boom(), served_model_loader=lambda: _ready("gpt-oss-120b")).run(
        owner
    )
    assert await store.night_hold_names(owner) == frozenset()  # freed despite the failure


async def test_run_without_a_served_model_loader_holds_nothing(maker: async_sessionmaker) -> None:
    # No local router (loader None) → the night runs unreserved rather than not at all.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _HoldWatchingExecutor(store, owner)
    await _runner(maker, store, executor).run(owner)
    assert executor.hold_during_turn == frozenset()
    assert await store.night_hold_names(owner) == frozenset()


class _PrologueCapturingExecutor(_FakeExecutor):
    """Records the prologue text of each sitting it runs, so a test can assert what was (and
    was not) injected into a given sitting."""

    def __init__(self) -> None:
        super().__init__()
        self.prologues: list[str] = []

    async def run_turn(self, **kwargs: object) -> ExecutedTurn:
        convo = cast("list[UserMessage]", kwargs.get("conversation") or [])
        # The sitting builds [now_block, prologue]; the prologue is the last user message.
        self.prologues.append(convo[-1].text if convo else "")
        return await super().run_turn(**kwargs)


async def test_advisory_note_rides_the_first_sitting_only(maker: async_sessionmaker) -> None:
    # The owner's advisory note is injected — framed as trusted-but-non-binding — into the
    # FIRST sitting's prologue, and NOT re-injected on later fresh sittings.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_advisory_note"] = "maybe look at the tide-pool submol tonight"
    executor = _PrologueCapturingExecutor()
    # step 600 → 5 sittings, so there are later sittings to check.
    await _runner(maker, store, executor, clock=_stepped_clock(600)).run(owner)

    assert len(executor.prologues) == 5
    first = executor.prologues[0]
    assert "A NOTE FROM YOUR HUMAN" in first
    assert "maybe look at the tide-pool submol tonight" in first
    assert "COMMENTS, not" in first  # the advisory (non-binding) framing
    for later in executor.prologues[1:]:
        assert "A NOTE FROM YOUR HUMAN" not in later
        assert "tide-pool submol" not in later


async def test_no_advisory_block_when_the_note_is_blank(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    store = FakeSettingsStore()  # no advisory note set
    executor = _PrologueCapturingExecutor()
    await _runner(maker, store, executor).run(owner)
    assert executor.prologues and "A NOTE FROM YOUR HUMAN" not in executor.prologues[0]


async def test_tick_self_heals_a_dangling_night_hold(maker: async_sessionmaker) -> None:
    # A prior night that died before its `finally` unwound leaves the hold set; the next tick
    # clears it (lane idle) so the box isn't stuck reserved all day.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_api_key"] = "moltbook_key123456"
    await store.set_night_hold_names(owner, ["gpt-oss-120b"])  # dangling from a crashed night
    lane = SingleFlightLane()
    # Out of window, so no new run launches — but the self-heal still fires.
    await jmolt_night_tick(
        maker,
        _runner(maker, store, _FakeExecutor()),
        store,  # type: ignore[arg-type]
        lane,
        now=datetime(2026, 8, 25, 14, 0, tzinfo=UTC),
    )
    assert await store.night_hold_names(owner) == frozenset()
