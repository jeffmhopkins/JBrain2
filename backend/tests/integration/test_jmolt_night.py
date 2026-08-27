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
    JMOLT_MAX_EMPTY_RETRIES,
    JMOLT_MAX_SITTINGS,
    JmoltNightRunner,
    SingleFlightLane,
    jmolt_night_tick,
    jmolt_run_context,
)
from jbrain.agent.loop import AgentResult
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import UserMessage
from jbrain.llm.errors import LlmStreamTruncatedError
from jbrain.models.jmolt_outbox import ActionLedgerRepo, OutboxRepo
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


class _DeadlineWatchingExecutor(_FakeExecutor):
    """Records the night deadline stamped WHILE its turn ran — what the `time_left` tool
    reads to tell jmolt how much of its hour is left."""

    def __init__(self, store: FakeSettingsStore, owner: SessionContext) -> None:
        super().__init__()
        self._store = store
        self._owner = owner
        self.deadline_during_turn = ""

    async def run_turn(self, **kwargs: object) -> ExecutedTurn:
        self.deadline_during_turn = await self._store.moltbook_night_deadline(self._owner)
        return await super().run_turn(**kwargs)


async def test_run_stamps_the_night_deadline_then_clears_it(maker: async_sessionmaker) -> None:
    # The deadline (woke_at + 1h) is set for the duration of the night so `time_left` can
    # report the minutes remaining, and cleared once the night ends.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _DeadlineWatchingExecutor(store, owner)
    await _runner(maker, store, executor).run(owner)

    assert executor.deadline_during_turn  # a deadline was stamped during the sitting
    # It is one hour past the stepped clock's wake time (2026-08-25 03:00 UTC).
    assert executor.deadline_during_turn.startswith("2026-08-25T04:00")
    assert await store.moltbook_night_deadline(owner) == ""  # cleared after the night


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


async def test_advisory_note_rides_every_sitting(maker: async_sessionmaker) -> None:
    # The owner's advisory note is injected — framed as trusted-but-non-binding — into EVERY
    # sitting's prologue. Each sitting is fresh-context with no memory of the last, so a note
    # left only on sitting 1 is gone for the rest of the night; re-supplying it is what lets a
    # note actually shape the whole hour (and survive to be acted on in the reflection sitting).
    owner = await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_advisory_note"] = "maybe look at the tide-pool submol tonight"
    executor = _PrologueCapturingExecutor()
    # step 600 → 5 sittings, so there are later sittings to check.
    await _runner(maker, store, executor, clock=_stepped_clock(600)).run(owner)

    assert len(executor.prologues) == 5
    for prologue in executor.prologues:
        assert "A NOTE FROM YOUR HUMAN" in prologue
        assert "maybe look at the tide-pool submol tonight" in prologue
        assert "COMMENTS, not" in prologue  # the advisory (non-binding) framing


async def test_no_advisory_block_when_the_note_is_blank(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    store = FakeSettingsStore()  # no advisory note set
    executor = _PrologueCapturingExecutor()
    await _runner(maker, store, executor).run(owner)
    assert executor.prologues and "A NOTE FROM YOUR HUMAN" not in executor.prologues[0]


async def test_the_handle_rides_every_sitting(maker: async_sessionmaker) -> None:
    # jmolt's registered handle is its NAME for the night, so it is injected into EVERY
    # sitting's prologue, never guessed and never lost mid-night.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_handle"] = "tidepool_jmolt"
    executor = _PrologueCapturingExecutor()
    await _runner(maker, store, executor, clock=_stepped_clock(600)).run(owner)  # 5 sittings

    assert len(executor.prologues) == 5
    for prologue in executor.prologues:
        assert "@tidepool_jmolt" in prologue
        assert "That handle is your name" in prologue


async def test_no_identity_block_when_no_handle_is_registered(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    store = FakeSettingsStore()  # no handle set
    executor = _PrologueCapturingExecutor()
    await _runner(maker, store, executor).run(owner)
    assert executor.prologues and "That handle is your name" not in executor.prologues[0]


class _EmptyFirstThenRealExecutor(_PrologueCapturingExecutor):
    """Returns an EMPTY turn (no final text, one model step, end_turn — the gpt-oss
    empty-final-channel quirk) on its FIRST call, then real turns. Lets a test observe the
    night's empty-sitting detect-and-retry."""

    async def run_turn(self, **kwargs: object) -> ExecutedTurn:
        convo = cast("list[UserMessage]", kwargs.get("conversation") or [])
        self.prologues.append(convo[-1].text if convo else "")
        self.calls += 1
        text = "" if self.calls == 1 else "I read my files and sat with a thread."
        steps = 1 if self.calls == 1 else 4
        return ExecutedTurn(
            result=AgentResult(text=text, stop_reason="end_turn", steps=steps, cost_tokens=0),
            tools=[],
            reasoning="We should list the scratch files.",
        )


class _AlwaysEmptyExecutor(_FakeExecutor):
    """Every sitting comes back empty — the wedged-model case. Proves the retry is BOUNDED
    (the night terminates instead of spinning the hour on retries)."""

    async def run_turn(self, **kwargs: object) -> ExecutedTurn:
        self.calls += 1
        return ExecutedTurn(
            result=AgentResult(text="", stop_reason="end_turn", steps=1, cost_tokens=0),
            tools=[],
            reasoning="We should list the scratch files.",
        )


async def test_empty_sitting_is_retried_with_a_nudge_and_not_counted(
    maker: async_sessionmaker,
) -> None:
    # A sitting that produced no work (empty final, one step) is re-run rather than counted:
    # the retry re-uses the SAME sitting number and carries a concrete first-move nudge.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _EmptyFirstThenRealExecutor()
    await _runner(maker, store, executor, clock=_stepped_clock(600)).run(owner)

    assert executor.calls >= 2  # the empty first sitting was retried, not skipped over
    # Only the retry carries the nudge, and it re-runs sitting 1 (the empty one took no slot).
    assert "produced nothing" not in executor.prologues[0]
    assert "produced nothing" in executor.prologues[1]
    assert "This is sitting 1." in executor.prologues[0]
    assert "This is sitting 1." in executor.prologues[1]
    # The second sitting number is only reached AFTER a real sitting-1 completed.
    assert any("This is sitting 2." in p for p in executor.prologues[2:])


class _AlternatingEmptyExecutor(_PrologueCapturingExecutor):
    """Empty, real, empty, real, … — an INTERMITTENT fault, which is what the live one is.
    The retry budget must survive it; a night-wide budget would be spent by sitting 6."""

    async def run_turn(self, **kwargs: object) -> ExecutedTurn:
        convo = cast("list[UserMessage]", kwargs.get("conversation") or [])
        self.prologues.append(convo[-1].text if convo else "")
        self.calls += 1
        empty = self.calls % 2 == 1
        return ExecutedTurn(
            result=AgentResult(
                text="" if empty else "I read my files and sat with a thread.",
                stop_reason="end_turn",
                steps=1 if empty else 4,
                cost_tokens=0 if empty else 900,
            ),
            tools=[],
            reasoning="We need to list files.",
        )


class _ZeroCostExecutor(_FakeExecutor):
    """A single-step turn carrying text but billing nothing — no usage chunk arrived, which
    on this box meant the stream was cut before the model's real output did. Multi-step
    zero-cost turns are deliberately NOT empty: see `_is_empty_sitting`."""

    async def run_turn(self, **_kwargs: object) -> ExecutedTurn:
        self.calls += 1
        return ExecutedTurn(
            result=AgentResult(
                text="I looked around.", stop_reason="end_turn", steps=1, cost_tokens=0
            ),
            tools=[],
            reasoning="",
        )


class _TransientThenRealExecutor(_PrologueCapturingExecutor):
    """Raises a transient provider fault on the first call, then works."""

    async def run_turn(self, **kwargs: object) -> ExecutedTurn:
        convo = cast("list[UserMessage]", kwargs.get("conversation") or [])
        self.prologues.append(convo[-1].text if convo else "")
        self.calls += 1
        if self.calls == 1:
            raise LlmStreamTruncatedError("local stream ended without a finish_reason")
        return ExecutedTurn(
            result=AgentResult(
                text="I read my files.", stop_reason="end_turn", steps=4, cost_tokens=900
            ),
            tools=[],
            reasoning="",
        )


async def test_empty_retry_budget_resets_after_a_productive_sitting(
    maker: async_sessionmaker,
) -> None:
    # THE REGRESSION. `empty_retries` was night-wide and never reset, so three empties
    # anywhere in the hour left the rest of the night unprotected — on 2026-08-27 the next
    # six empties each burned a real slot and the whole budget went in under nine minutes.
    # Alternating empty/real means a night-wide budget is spent by sitting 6; a consecutive
    # one never runs out.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _AlternatingEmptyExecutor()
    await _runner(maker, store, executor, clock=_stepped_clock(200)).run(owner)

    # Every empty was retried rather than counted. Under the OLD night-wide budget the
    # alternating fault would spend all three retries by call 6 and then burn a real slot on
    # every subsequent empty; with a consecutive budget none of them is ever counted, so the
    # night is bounded by its clock rather than eaten by the fault.
    retried = [p for p in executor.prologues if "produced nothing" in p]
    assert len(retried) == len(executor.prologues) // 2  # every empty got a retry, none counted
    assert "not for the feed" in executor.prologues[-1]  # and it still closes on reflection


async def test_a_wedged_model_does_not_rearm_the_retry_budget_each_slot(
    maker: async_sessionmaker,
) -> None:
    # "Consecutive" has to mean consecutive. Resetting after ANY sitting — including an empty
    # one that already burned a slot — would give a permanently wedged model three fresh
    # retries per slot (~48 attempts across the budget) instead of three in a row.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _AlwaysEmptyExecutor()
    await _runner(maker, store, executor, clock=_stepped_clock(60)).run(owner)

    assert executor.calls <= JMOLT_MAX_SITTINGS + JMOLT_MAX_EMPTY_RETRIES + 1


async def test_a_zero_cost_sitting_is_retried_not_counted(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _ZeroCostExecutor()
    await _runner(maker, store, executor, clock=_stepped_clock(60)).run(owner)

    # It looks productive but billed nothing, so it is treated as empty: retried, then
    # bounded by the consecutive cap rather than spending the night.
    assert executor.calls <= JMOLT_MAX_SITTINGS + JMOLT_MAX_EMPTY_RETRIES + 1


async def test_a_transient_llm_fault_is_retried_like_an_empty_sitting(
    maker: async_sessionmaker,
) -> None:
    # A cut stream the adapter could not recover reaches the night as LlmTransientError.
    # The round was never committed, so it must hand the slot back, not burn it.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _TransientThenRealExecutor()
    await _runner(maker, store, executor, clock=_stepped_clock(600)).run(owner)

    assert "produced nothing" in executor.prologues[1]  # retried, with the first-move nudge
    assert "This is sitting 1." in executor.prologues[0]
    assert "This is sitting 1." in executor.prologues[1]  # the slot was handed back
    assert any("This is sitting 2." in p for p in executor.prologues[2:])


async def test_empty_sittings_stop_retrying_at_the_cap(maker: async_sessionmaker) -> None:
    # A wedged model (every sitting empty) must not loop the hour away on retries: after
    # JMOLT_MAX_EMPTY_RETRIES the empties count as ordinary sittings and the night ends.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _AlwaysEmptyExecutor()
    # A 60s step leaves ~55 iterations of clock headroom, so the retry cap — not the time
    # bound — is what has to stop this night. At 300s the clock ended it first and the
    # assertion below passed without ever exercising the cap.
    await _runner(maker, store, executor, clock=_stepped_clock(60)).run(owner)

    # Bounded: at most the sitting budget plus the extra retries — never an unbounded spin.
    assert executor.calls <= JMOLT_MAX_SITTINGS + JMOLT_MAX_EMPTY_RETRIES


async def test_the_last_sitting_is_reserved_for_reflection(maker: async_sessionmaker) -> None:
    # As the hour closes, one sitting is reserved for reflection — thinking + files, not the
    # feed — and it is the night's last. The reflection prologue rides only that sitting.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _PrologueCapturingExecutor()
    # step 600 → sittings at elapsed 600..3000; the reflection window opens at 3000 (WALL-600),
    # so the 5th sitting is the reflection sitting and the night ends after it.
    await _runner(maker, store, executor, clock=_stepped_clock(600)).run(owner)

    assert len(executor.prologues) == 5
    for earlier in executor.prologues[:-1]:
        assert "not for the feed" not in earlier
    last = executor.prologues[-1]
    assert "not for the feed" in last  # the reflection sitting
    assert "threads you actually mean to pull tomorrow" in last


class _EmptyReflectionThenRealExecutor(_PrologueCapturingExecutor):
    """Comes back EMPTY on the first sitting that carries the reflection prologue, then real.
    Lets a test prove the reflection slot survives its own empty-sitting retry."""

    def __init__(self) -> None:
        super().__init__()
        self._seen_reflection = False

    async def run_turn(self, **kwargs: object) -> ExecutedTurn:
        convo = cast("list[UserMessage]", kwargs.get("conversation") or [])
        prologue = convo[-1].text if convo else ""
        self.prologues.append(prologue)
        self.calls += 1
        reflection = "not for the feed" in prologue
        first_reflection = reflection and not self._seen_reflection
        self._seen_reflection = self._seen_reflection or reflection
        return ExecutedTurn(
            result=AgentResult(
                text="" if first_reflection else "I read back what I wrote and wrote what I think.",
                stop_reason="end_turn",
                steps=1 if first_reflection else 4,
                cost_tokens=0,
            ),
            tools=[],
            reasoning="",
        )


async def test_reflection_still_runs_when_the_sitting_budget_is_spent_first(
    maker: async_sessionmaker,
) -> None:
    # THE REGRESSION. The closing reflection sitting used to be gated on elapsed time while
    # the loop itself was bounded by JMOLT_MAX_SITTINGS, so a night of QUICK sittings spent
    # the budget before the time window opened and exited — no reflection, ever. Measured on
    # the box: two real nights, 13 sittings, zero reflections; the 2026-08-26 night used all
    # 12 slots by minute 40 (~200 s each) and stopped 20 minutes early.
    #
    # step 200 reproduces exactly that pace: the budget is spent at elapsed 2400, well short
    # of the 3000 s reflection window and the 3300 s hard stop.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _PrologueCapturingExecutor()
    await _runner(maker, store, executor, clock=_stepped_clock(200)).run(owner)

    # The budget bounds the FEED sittings; the closing sitting is extra, so 12 + 1.
    assert len(executor.prologues) == JMOLT_MAX_SITTINGS + 1
    for feed in executor.prologues[:-1]:
        assert "not for the feed" not in feed
    assert "not for the feed" in executor.prologues[-1]
    assert "threads you actually mean to pull tomorrow" in executor.prologues[-1]


async def test_a_killed_night_gets_no_reflection_sitting(maker: async_sessionmaker) -> None:
    # The kill outranks the reserved sitting: reflection is a stretch of the hour, not a debt
    # the night owes itself, so a night stopped mid-flight ends where it was stopped.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    store.values["moltbook_killed"] = True
    executor = _PrologueCapturingExecutor()
    await _runner(maker, store, executor, clock=_stepped_clock(200)).run(owner)

    assert len(executor.prologues) == 1
    assert "not for the feed" not in executor.prologues[0]


async def test_an_empty_reflection_sitting_is_retried_as_a_reflection_sitting(
    maker: async_sessionmaker,
) -> None:
    # The empty-sitting retry hands the slot back (`sitting -= 1`), which un-spends the budget
    # that made the reflection due. The flag latches so the retry stays a reflection sitting
    # rather than silently dropping back to the feed prologue for the night's last stretch.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _EmptyReflectionThenRealExecutor()
    await _runner(maker, store, executor, clock=_stepped_clock(200)).run(owner)

    reflections = [p for p in executor.prologues if "not for the feed" in p]
    assert len(reflections) == 2  # the empty one, then its retry — still reflection
    assert "produced nothing" in reflections[-1]  # and it carried the concrete-first-move nudge
    assert "not for the feed" in executor.prologues[-1]  # the night still ENDS on reflection


async def test_the_night_never_exceeds_the_budget_plus_its_closing_sitting(
    maker: async_sessionmaker,
) -> None:
    # Removing the `sitting < JMOLT_MAX_SITTINGS` loop bound must not make the night
    # unbounded: a very fast clock still terminates at the budget plus the one closing sitting.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    executor = _FakeExecutor()
    await _runner(maker, store, executor, clock=_stepped_clock(1)).run(owner)

    assert executor.calls == JMOLT_MAX_SITTINGS + 1


async def test_done_tonight_block_names_targets_not_just_counts(
    maker: async_sessionmaker,
) -> None:
    # The old block said "2 comments, 1 vote" — a number, which cannot stop a duplicate. The
    # only useful fact is WHICH POST, because jmolt's own writes are invisible when it reads
    # the thread back.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    async with scoped_session(maker, jmolt_run_context(owner.principal_id)) as s:
        await ActionLedgerRepo().record(
            s, owner.principal_id, action="stage_comment", target="post-abc"
        )
        await ActionLedgerRepo().record(
            s, owner.principal_id, action="stage_comment", target="post-abc"
        )
        await ActionLedgerRepo().record(
            s, owner.principal_id, action="stage_vote", target="post-xyz"
        )
    executor = _PrologueCapturingExecutor()
    await _runner(maker, store, executor).run(owner)

    first = executor.prologues[0]
    assert "post-abc" in first and "post-xyz" in first
    assert "comment 2x on post-abc" in first  # repetition shown AS repetition
    assert "NOT visible on the site yet" in first


async def test_done_tonight_block_counts_published_rows_too(maker: async_sessionmaker) -> None:
    # The old block read the OUTBOX, and only its queued/released rows. The drip publishes
    # 20-45s after staging, so rows fell out of it almost immediately and it reported
    # near-nothing. Reading the LEDGER instead makes the outbox row's later status
    # irrelevant — what jmolt did is recorded once and stays recorded.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    async with scoped_session(maker, jmolt_run_context(owner.principal_id)) as s:
        await ActionLedgerRepo().record(
            s, owner.principal_id, action="stage_comment", target="post-published"
        )
        await OutboxRepo().stage(
            s,
            owner.principal_id,
            kind="comment",
            payload={"post_id": "post-published", "content": "hi"},
        )
    executor = _PrologueCapturingExecutor()
    await _runner(maker, store, executor).run(owner)
    assert "post-published" in executor.prologues[0]


async def test_done_tonight_block_ignores_earlier_nights(maker: async_sessionmaker) -> None:
    # Scoped to THIS night: yesterday's actions are not repetition tonight, and a block that
    # grew without bound would crowd out the prologue it precedes.
    owner = await _owner(maker)
    store = FakeSettingsStore()
    async with scoped_session(maker, jmolt_run_context(owner.principal_id)) as s:
        await s.execute(
            text(
                "INSERT INTO app.jmolt_action_ledger (principal_id, action, target, at)"
                " VALUES (:pid, 'stage_comment', 'post-yesterday', now() - interval '2 days')"
            ),
            {"pid": owner.principal_id},
        )
    executor = _PrologueCapturingExecutor()
    await _runner(maker, store, executor).run(owner)
    assert "post-yesterday" not in executor.prologues[0]


async def test_done_tonight_block_is_absent_when_nothing_has_happened(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner(maker)
    executor = _PrologueCapturingExecutor()
    await _runner(maker, FakeSettingsStore(), executor).run(owner)
    assert "ALREADY DONE TONIGHT" not in executor.prologues[0]


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
