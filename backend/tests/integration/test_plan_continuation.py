"""Plan auto-continuation against real Postgres (docs/plans/JERV_PLANNING_TOOL_PLAN.md).

Covers the continuation bookkeeping on `agent_session_plans`: scheduling + atomic
claim, the guards that keep a claim from firing (awaiting-owner, not-in-work, over the
cap), the shared settle decision (`maybe_schedule_continuation`), the owner-message
reset, jerv's `await_owner` opt-out through the write_plan handler, and that only a
session's ACTIVE plan is run (a superseded plan is skipped).
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.continuation import (
    CONTINUATION_DELAY_S,
    MAX_CONTINUATIONS,
    PRESENCE_TTL_S,
    PlanContinuationRunner,
    maybe_schedule_continuation,
)
from jbrain.agent.live_turn import _LiveTurn
from jbrain.agent.loop import ToolContext
from jbrain.agent.plantools import build_plan_handlers
from jbrain.agent.session import AgentSessionRepo
from jbrain.api.agent import _plan_blocks
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.plan import PlanRepo
from jbrain.tasks.scheduler import _owner_principal_id
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_OPEN = "- [ ] step one\n- [ ] step two"  # has unchecked items
_DONE = "- [x] step one\n- [x] step two"  # all checked


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


async def _chat(
    maker: async_sessionmaker, owner: SessionContext, *, body: str, status: str
) -> tuple[str, str]:
    """A jerv chat with one plan; returns (session_id, plan_id)."""
    sid = str(uuid.uuid4())
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.agent_sessions (id, principal_id, agent, domain_scopes)"
                " VALUES (:id, :pid, 'jerv', '{}')"
            ),
            {"id": sid, "pid": owner.principal_id},
        )
        plan = await PlanRepo().create(session, sid, body=body, status=status)
    return sid, str(plan.plan_id)


async def test_schedule_then_claim_fires_once_and_counts(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="in_work")
    repo = PlanRepo()

    async with scoped_session(maker, owner) as s:
        await repo.schedule_continuation(s, pid, delay_s=0)
        plan = await repo.get_by_id(s, pid)
        assert plan is not None and plan.continuation_due_at is not None

    async with scoped_session(maker, owner) as s:
        claimed = await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS)
    assert claimed == [(pid, sid)]  # claim returns (plan_id, session_id) pairs

    async with scoped_session(maker, owner) as s:
        plan = await repo.get_by_id(s, pid)
        assert plan is not None
        assert plan.continuation_due_at is None  # claim cleared the due-time…
        assert plan.continuations_used == 0  # …but the count is bumped on a real run, not claim
        # A second sweep claims nothing (the due-time is gone).
        assert await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS) == []

    # bump_continuation (called when the claimed turn actually runs) counts the fire.
    async with scoped_session(maker, owner) as s:
        await repo.bump_continuation(s, pid)
    async with scoped_session(maker, owner) as s:
        plan = await repo.get_by_id(s, pid)
        assert plan is not None and plan.continuations_used == 1


async def test_claim_skips_awaiting_owner_and_non_in_work(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    _, awaiting = await _chat(maker, owner, body=_OPEN, status="in_work")
    _, draft = await _chat(maker, owner, body=_OPEN, status="not_approved")
    repo = PlanRepo()

    async with scoped_session(maker, owner) as s:
        await repo.schedule_continuation(s, awaiting, delay_s=0)
        await repo.set_awaiting_owner(s, awaiting, True)  # also clears the due-time
        await repo.schedule_continuation(s, draft, delay_s=0)  # but it's not in_work

    async with scoped_session(maker, owner) as s:
        assert await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS) == []


async def test_claim_respects_the_cap(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    _, pid = await _chat(maker, owner, body=_OPEN, status="in_work")
    repo = PlanRepo()
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text("UPDATE app.agent_session_plans SET continuations_used = :m WHERE plan_id = :id"),
            {"m": MAX_CONTINUATIONS, "id": pid},
        )
        await repo.schedule_continuation(s, pid, delay_s=0)
    async with scoped_session(maker, owner) as s:
        assert await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS) == []


async def _due(maker: async_sessionmaker, owner: SessionContext, sid: str) -> bool:
    async with scoped_session(maker, owner) as s:
        plan = await PlanRepo().get_active(s, sid)
        assert plan is not None
        return plan.continuation_due_at is not None


async def test_settle_helper_arms_only_when_it_should(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)

    # in-work + unchecked + clean stop → armed.
    good, _ = await _chat(maker, owner, body=_OPEN, status="in_work")
    await maybe_schedule_continuation(maker, owner, good, agent="jerv", stop_reason="end_turn")
    assert await _due(maker, owner, good)

    # approved-but-not-yet-started + unchecked → armed too (the first step continues the
    # plan without the owner having to send another message).
    approved, _ = await _chat(maker, owner, body=_OPEN, status="approved")
    await maybe_schedule_continuation(maker, owner, approved, agent="jerv", stop_reason="end_turn")
    assert await _due(maker, owner, approved)

    # a still-unapproved draft is never armed.
    draft, _ = await _chat(maker, owner, body=_OPEN, status="not_approved")
    await maybe_schedule_continuation(maker, owner, draft, agent="jerv", stop_reason="end_turn")
    assert not await _due(maker, owner, draft)

    # all steps checked → not armed.
    done, _ = await _chat(maker, owner, body=_DONE, status="in_work")
    await maybe_schedule_continuation(maker, owner, done, agent="jerv", stop_reason="end_turn")
    assert not await _due(maker, owner, done)

    # a deferred turn (handed off to a background job) → not armed.
    deferred, _ = await _chat(maker, owner, body=_OPEN, status="in_work")
    await maybe_schedule_continuation(maker, owner, deferred, agent="jerv", stop_reason="deferred")
    assert not await _due(maker, owner, deferred)

    # a non-jerv agent → not armed.
    other, _ = await _chat(maker, owner, body=_OPEN, status="in_work")
    await maybe_schedule_continuation(maker, owner, other, agent="curator", stop_reason="end_turn")
    assert not await _due(maker, owner, other)


class _FakeResult:
    text = "worked the step"
    stop_reason = "end_turn"
    steps = 1
    cost_tokens = 5


class _FakeExecuted:
    result = _FakeResult()
    tools: list = []
    reasoning = ""


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run_turn(self, **kwargs: object) -> _FakeExecuted:
        # Record the session and the TYPE of the principal id — the uuid→str bug would
        # never reach here (tick would throw at set_config), so a call at all proves the fix.
        # `supervised` proves the presence-gated budget path (a watched step runs unbounded).
        pid = kwargs["read_ctx"].principal_id  # type: ignore[attr-defined]
        self.calls.append(
            {
                "sid": kwargs["agent_session_id"],
                "pid_type": type(pid).__name__,
                "supervised": kwargs.get("supervised"),
            }
        )
        return _FakeExecuted()


class _FakeRunLog:
    async def start(self, ctx: object, *, session_id: str, prompt_version: str) -> str:
        return "run-1"

    def bound(self, ctx: object, run_id: str) -> object:
        return object()

    async def finish(self, ctx: object, run_id: str, **kwargs: object) -> None:
        return None


class _FakeTranscript:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def record_answer(self, ctx: object, *, session_id: str, **kwargs: object) -> None:
        self.answers.append(session_id)


async def test_sweep_tick_fires_a_due_continuation(maker: async_sessionmaker) -> None:
    """End-to-end sweep: with a real owner + an armed plan, `runner.tick()` claims and runs
    the continuation. Uses the REAL owner-pid resolver (a raw uuid) — the regression guard
    for the bug where the sweep passed that uuid straight into the RLS ctx and silently
    threw at set_config, so an approved plan sat at 'continuing in 0:00' forever."""
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="approved")
    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, pid, delay_s=0)

    executor = _FakeExecutor()
    transcript = _FakeTranscript()
    runner = PlanContinuationRunner(
        maker=maker,
        executor=executor,  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=transcript,  # type: ignore[arg-type]
        live_turns={},
        owner_principal_id=lambda: _owner_principal_id(maker),  # raw uuid, like production
    )
    await runner.tick()

    assert [c["sid"] for c in executor.calls] == [sid]  # claimed + ran (didn't throw)
    assert executor.calls[0]["pid_type"] == "str"  # the RLS ctx got a str, not a uuid
    # No client presence registered → the step runs on the ordinary bounded budget.
    assert executor.calls[0]["supervised"] is False
    assert transcript.answers == [sid]  # recorded answer-only
    async with scoped_session(maker, owner) as s:
        plan = await PlanRepo().get_by_id(s, pid)
        assert plan is not None and plan.continuations_used == 1  # the fire was counted


class _ContextExecuted:
    result = _FakeResult()
    tools: list = []
    reasoning = ""
    context_used = 1500  # the fullest step's prompt+output
    context_window = 5000  # the model's window it ran against


class _ContextFakeExecutor:
    async def run_turn(self, **_kwargs: object) -> _ContextExecuted:
        return _ContextExecuted()


async def test_continuation_persists_the_context_meter_seed(maker: async_sessionmaker) -> None:
    """The meter fix (Gap #2): after a continuation turn settles, the runner persists the turn's
    context fill onto the AgentSession row via `record_context`, so a client that reopens the
    chat AFTER the live stream is gone still sees the true context usage instead of a stale
    foreground value. Gated on `sessions` being wired (production wires it in main.py)."""
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="approved")
    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, pid, delay_s=0)

    runner = PlanContinuationRunner(
        maker=maker,
        executor=_ContextFakeExecutor(),  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=_FakeTranscript(),  # type: ignore[arg-type]
        live_turns={},
        owner_principal_id=lambda: _owner_principal_id(maker),
        sessions=AgentSessionRepo(maker),
    )
    await runner.tick()

    async with scoped_session(maker, owner) as s:
        row = (
            await s.execute(
                text(
                    "SELECT context_tokens, context_window FROM app.agent_sessions WHERE id = :id"
                ),
                {"id": sid},
            )
        ).one()
    assert row.context_tokens == 1500  # the meter seed the continuation persisted
    assert row.context_window == 5000


async def test_superseded_plan_is_not_run(maker: async_sessionmaker) -> None:
    """The claim→supersede race: a continuation already claimed for a plan that is no longer the
    session's ACTIVE plan (a newer plan was drafted after the claim) must NOT run — its tools
    would resolve to the newer active plan, doing cross-plan work. `_run_one` re-reads the active
    plan under its reservation and drops the stale one. Driven through `_run_one` directly because
    `create`'s supersede clears the old plan's due-time, so a `tick()` claim would never reach the
    guard — this exercises the guard itself, not the supersede."""
    owner = await _owner(maker)
    sid, old_pid = await _chat(maker, owner, body=_OPEN, status="in_work")
    # The old plan is still in_work with open items (so only the active-guard can stop it), but a
    # newer plan now exists — so old_pid is no longer active.
    async with scoped_session(maker, owner) as s:
        await PlanRepo().create(s, sid, body="- [ ] newer", status="not_approved")

    executor = _FakeExecutor()
    runner = PlanContinuationRunner(
        maker=maker,
        executor=executor,  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=_FakeTranscript(),  # type: ignore[arg-type]
        live_turns={},
        owner_principal_id=lambda: _owner_principal_id(maker),
    )
    # Simulate a claim that already happened, then run the claimed (now-superseded) plan.
    await runner._run_one(old_pid, sid, owner.principal_id, owner)

    # The active-plan guard (active_id != old_pid) dropped it before the executor ran.
    assert executor.calls == []


async def test_schedule_kick_runs_a_due_step_immediately(maker: async_sessionmaker) -> None:
    """The owner approving (or hitting Continue) fires an on-demand kick so the due step runs
    NOW — not on the next periodic sweep. schedule_kick spawns a tracked task that runs one
    tick; draining it runs the claimed step."""
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="approved")
    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, pid, delay_s=0)

    executor = _FakeExecutor()
    runner = PlanContinuationRunner(
        maker=maker,
        executor=executor,  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=_FakeTranscript(),  # type: ignore[arg-type]
        live_turns={},
        owner_principal_id=lambda: _owner_principal_id(maker),
    )
    runner.schedule_kick()
    tasks = list(runner._kick_tasks)
    assert tasks  # a kick task was scheduled and tracked (so it isn't GC'd)
    await asyncio.gather(*tasks)

    assert [c["sid"] for c in executor.calls] == [sid]  # ran now, no sweep tick needed


async def test_busy_session_rearms_promptly_not_the_owner_window(
    maker: async_sessionmaker,
) -> None:
    """A due step whose session is busy (an owner turn holds it — e.g. the owner approved while
    the draft turn was still finishing) does NOT idle the full 60s owner window: it re-arms
    due-now so the next sweep retries the instant the session frees. The 60s countdown bug."""
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="approved")
    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, pid, delay_s=0)

    executor = _FakeExecutor()
    live_turns: dict = {"owner-run": _LiveTurn(session_id=sid)}  # a live owner turn holds it
    runner = PlanContinuationRunner(
        maker=maker,
        executor=executor,  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=_FakeTranscript(),  # type: ignore[arg-type]
        live_turns=live_turns,
        owner_principal_id=lambda: _owner_principal_id(maker),
    )
    await runner.tick()

    assert executor.calls == []  # busy → the step did not run
    async with scoped_session(maker, owner) as s:
        plan = await PlanRepo().get_by_id(s, pid)
        assert plan is not None and plan.continuation_due_at is not None  # re-armed
        # Re-armed to retry promptly (due ~now), NOT the 60s owner window.
        ahead = (plan.continuation_due_at - datetime.now(UTC)).total_seconds()
        assert ahead < CONTINUATION_DELAY_S / 2
    # Clear the due-now re-arm so it can't be claimed by a later test's sweep (these tests
    # share one owner, and claim_due_continuations claims every due plan for that owner).
    async with scoped_session(maker, owner) as s:
        await PlanRepo().cancel_and_reset(s, pid)


async def test_continuation_runs_supervised_when_a_client_is_present(
    maker: async_sessionmaker,
) -> None:
    """A step fired while a foreground client is watching (a recent live-run poll) runs
    SUPERVISED — the lifted per-turn budget — so a long step isn't cut off mid-work."""
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="approved")
    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, pid, delay_s=0)

    executor = _FakeExecutor()
    runner = PlanContinuationRunner(
        maker=maker,
        executor=executor,  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=_FakeTranscript(),  # type: ignore[arg-type]
        live_turns={},
        owner_principal_id=lambda: _owner_principal_id(maker),
        client_presence={sid: time.monotonic()},  # a fresh foreground live-run poll
    )
    await runner.tick()

    assert executor.calls[0]["supervised"] is True


async def test_continuation_unsupervised_when_presence_is_stale(
    maker: async_sessionmaker,
) -> None:
    """A step fired after the app was backgrounded/closed (its live-run poll stopped, so
    presence aged past the TTL) falls back to the ordinary bounded budget."""
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="approved")
    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, pid, delay_s=0)

    executor = _FakeExecutor()
    runner = PlanContinuationRunner(
        maker=maker,
        executor=executor,  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=_FakeTranscript(),  # type: ignore[arg-type]
        live_turns={},
        owner_principal_id=lambda: _owner_principal_id(maker),
        client_presence={sid: time.monotonic() - (PRESENCE_TTL_S + 5)},  # last seen too long ago
    )
    await runner.tick()

    assert executor.calls[0]["supervised"] is False


class _StreamEvent:
    """A minimal ChatEvent stand-in for the continuation's SSE sink — only model_dump_json
    is used (to build the `data:` frame)."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump_json(self) -> str:
        return json.dumps(self._payload)


class _StreamingFakeExecutor:
    """A fake that STREAMS: it drives the `on_event` sink the continuation passes, so the
    turn's `_LiveTurn` broker fills with `data:` frames — the reattach path a foreground
    client rides. Snapshots the shared live-turns registry mid-run (before `_run_one` pops
    it in its finally), which is exactly what `session_live_run` reads to discover the run."""

    def __init__(self, live_turns: dict) -> None:
        self._live_turns = live_turns
        self.during: dict = {}

    async def run_turn(self, **kwargs: object) -> _FakeExecuted:
        on_event = kwargs.get("on_event")
        if on_event is not None:
            on_event(_StreamEvent({"type": "text_delta", "text": "on it"}))  # type: ignore[operator]
            on_event(_StreamEvent({"type": "done", "stop_reason": "stop"}))  # type: ignore[operator]
        turns = list(self._live_turns.items())
        self.during = {
            "keys": [k for k, _ in turns],
            "sessions": [getattr(v, "session_id", None) for _, v in turns],
            "frames": [list(getattr(v, "frames", [])) for _, v in turns],
            "has_acc": [getattr(v, "acc", None) is not None for _, v in turns],
        }
        return _FakeExecuted()


async def test_continuation_streams_via_a_live_turn(maker: async_sessionmaker) -> None:
    """A continuation registers a REAL `_LiveTurn` keyed by its run_id and emits each event as
    a `data:` SSE frame — so a foreground/reloaded client discovers it (`session_live_run`
    reads `live_turns` by session_id) and streams the step live, then the registry is drained
    when the turn ends. The regression guard for 'plan steps run invisibly'."""
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="approved")
    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, pid, delay_s=0)

    live_turns: dict = {}
    executor = _StreamingFakeExecutor(live_turns)
    runner = PlanContinuationRunner(
        maker=maker,
        executor=executor,  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=_FakeTranscript(),  # type: ignore[arg-type]
        live_turns=live_turns,
        owner_principal_id=lambda: _owner_principal_id(maker),
    )
    await runner.tick()

    # DURING the run: exactly one live turn, keyed by the run_id ("run-1" from _FakeRunLog) so
    # session_live_run / resume_chat_run find it, tagged with the chat session, holding an acc
    # snapshot and the two emitted SSE frames.
    d = executor.during
    assert d["keys"] == ["run-1"]
    assert d["sessions"] == [sid]
    assert d["has_acc"] == [True]
    frames = d["frames"][0]
    assert len(frames) == 2
    assert frames[0].startswith(b"data: ") and frames[0].endswith(b"\n\n")
    assert b'"text": "on it"' in frames[0]

    # AFTER the run: the registry is drained (turn finished, buffer freed) — no leak.
    assert live_turns == {}


async def test_approving_arms_and_claims_the_first_step(maker: async_sessionmaker) -> None:
    """The fix for 'approved but sitting there': the approve endpoint arms a continuation,
    and the sweep claims an `approved` plan (not just `in_work`) — so the first step starts
    on its own. jerv then flips the status to in_work as it executes."""
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="approved")
    repo = PlanRepo()
    # Mirror the approve endpoint: arm the first continuation on the just-approved plan.
    async with scoped_session(maker, owner) as s:
        await repo.schedule_continuation(s, pid, delay_s=0)
        plan = await repo.get_by_id(s, pid)
        assert plan is not None and plan.continuation_due_at is not None
    # The sweep claims it even though the status is still `approved` (jerv hasn't started).
    async with scoped_session(maker, owner) as s:
        claimed = await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS)
        assert claimed == [(pid, sid)]


async def test_owner_message_reset_clears_everything(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    _, pid = await _chat(maker, owner, body=_OPEN, status="in_work")
    repo = PlanRepo()
    async with scoped_session(maker, owner) as s:
        await repo.schedule_continuation(s, pid, delay_s=0)
        await repo.set_awaiting_owner(s, pid, True)
        await s.execute(
            text("UPDATE app.agent_session_plans SET continuations_used = 5 WHERE plan_id = :id"),
            {"id": pid},
        )
        await repo.cancel_and_reset(s, pid)
        plan = await repo.get_by_id(s, pid)
        assert plan is not None
    assert plan.continuation_due_at is None
    assert plan.awaiting_owner is False
    assert plan.continuations_used == 0


async def test_plan_blocks_only_injects_a_sanctioned_plan(maker: async_sessionmaker) -> None:
    """The re-injection rule: a not_approved DRAFT is never fed to the turn as a sanctioned
    operating instruction (that's what stops an injection-drafted plan being framed as
    'follow this'); only an owner-approved / in-work plan is, and only for a jerv session."""
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body="- [ ] do the thing", status="not_approved")
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_maker=maker)))
    jerv = SimpleNamespace(agent="jerv", id=sid)

    # A draft is NOT injected.
    assert await _plan_blocks(req, owner, jerv) == []  # type: ignore[arg-type]

    # Once the owner approves, it IS injected — a DATA-framed operating block.
    async with scoped_session(maker, owner) as s:
        await PlanRepo().set_status(s, pid, "approved")
    blocks = await _plan_blocks(req, owner, jerv)  # type: ignore[arg-type]
    assert len(blocks) == 1
    assert "APPROVED" in blocks[0].text and "do the thing" in blocks[0].text  # type: ignore[union-attr]

    # A non-jerv agent never receives it.
    assert await _plan_blocks(req, owner, SimpleNamespace(agent="curator", id=sid)) == []  # type: ignore[arg-type]


class _RootTreeExecutor:
    """Records the `root_tree` flag the continuation drove run_turn with — the wire-up that
    seeds the turn as a fan root so its steps may call deep_research (the executor-level test
    proves the flag actually mints a TreeState)."""

    def __init__(self) -> None:
        self.root_tree: object = None

    async def run_turn(self, **kwargs: object) -> _FakeExecuted:
        self.root_tree = kwargs.get("root_tree")
        return _FakeExecuted()


async def test_continuation_opts_into_a_root_tree(maker: async_sessionmaker) -> None:
    """A continuation is an owner-approved turn, so it runs as a fan ROOT (`root_tree=True`) —
    otherwise `ctx.tree` is None and every deep_research/spawn step in the plan refuses with
    'only available in an interactive owner turn'. The regression guard for candidate-report
    plans (run candidate_profile per candidate, then compare_candidates) never getting off step
    one."""
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="approved")
    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, pid, delay_s=0)

    executor = _RootTreeExecutor()
    runner = PlanContinuationRunner(
        maker=maker,
        executor=executor,  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=_FakeTranscript(),  # type: ignore[arg-type]
        live_turns={},
        owner_principal_id=lambda: _owner_principal_id(maker),
    )
    await runner.tick()

    assert executor.root_tree is True


class _BlockingExecutor:
    """A turn that blocks until its task is cancelled — so a test can hit Stop mid-step and
    assert the loop halts instead of running the step to completion and re-arming."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run_turn(self, **_kwargs: object) -> _FakeExecuted:
        self.started.set()
        await asyncio.sleep(3600)  # until cancelled by the Stop
        return _FakeExecuted()  # never reached


async def test_stop_cancels_the_running_step_and_halts_the_loop(maker: async_sessionmaker) -> None:
    """The owner's Stop (POST /chat/runs/{run_id}/cancel → live.cancel()) must actually stop a
    plan: the continuation now runs its turn as a cancellable task published on `live.task`, and
    a cancelled turn marks the plan awaiting-owner so the sweep can't re-claim it and the settle
    helper can't re-arm. The regression guard for 'I hit Stop and it just keeps firing steps'."""
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="approved")
    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, pid, delay_s=0)

    executor = _BlockingExecutor()
    live_turns: dict = {}
    runner = PlanContinuationRunner(
        maker=maker,
        executor=executor,  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=_FakeTranscript(),  # type: ignore[arg-type]
        live_turns=live_turns,
        owner_principal_id=lambda: _owner_principal_id(maker),
    )
    run = asyncio.ensure_future(runner._run_one(pid, sid, owner.principal_id, owner))
    await asyncio.wait_for(executor.started.wait(), timeout=5)

    # The live turn is registered and carries the turn task, so the Stop endpoint can cancel it —
    # the bug was `live.task` staying None, which made live.cancel() a no-op for a continuation.
    live = next(lt for lt in live_turns.values() if not lt.done)
    assert live.task is not None
    live.cancel()  # the owner hits Stop
    await asyncio.wait_for(run, timeout=5)

    async with scoped_session(maker, owner) as s:
        plan = await PlanRepo().get_by_id(s, pid)
        assert plan is not None
        assert plan.awaiting_owner is True  # halted
        assert plan.continuation_due_at is None  # no next step armed
        # A sweep now skips it (the awaiting-owner claim filter), so the loop stays stopped.
        claimed = await PlanRepo().claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS)
        assert claimed == []
    assert live_turns == {}  # the registry drained — no leak


async def test_write_plan_await_owner_stops_the_loop(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sid, pid = await _chat(maker, owner, body=_OPEN, status="in_work")
    handlers = build_plan_handlers(maker)
    ctx = ToolContext(session=owner, scopes=(), agent_session_id=sid)

    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, pid, delay_s=0)

    out = await handlers["write_plan"]({"pause": "await_owner"}, ctx)
    assert "wait for your reply" in out

    async with scoped_session(maker, owner) as s:
        plan = await PlanRepo().get_by_id(s, pid)
        assert plan is not None
        assert plan.awaiting_owner is True
        assert plan.continuation_due_at is None  # await_owner also cancels a pending fire
        # …and a due sweep now skips it.
        claimed = await PlanRepo().claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS)
        assert claimed == []
