"""LoopTurnExecutor.run_turn's optional streaming sink (`acc` + `on_event`) — the seam that
lets a plan continuation stream its events onto a `_LiveTurn` broker while still folding the
durable transcript shape — and its context-usage accounting (the meter fix): it resolves the
window and passes it into run_stream so the loop EMITS UsageEvents, then returns the fullest
step's usage for the caller to persist. AgentLoop is faked; no LLM, no DB."""

from types import SimpleNamespace

import pytest

import jbrain.tasks.runner as runner_mod
from jbrain.agent.agents import agent_for
from jbrain.agent.contracts import UsageEvent
from jbrain.agent.tree import TreeState
from jbrain.db.session import SessionContext
from jbrain.tasks.runner import LoopTurnExecutor

OWNER = SessionContext(principal_id="11111111-1111-1111-1111-111111111111", principal_kind="owner")


class _Ev:
    """A minimal ChatEvent stand-in: `type` for the accumulator, `model_dump_json` for the
    SSE sink."""

    def __init__(self, kind: str) -> None:
        self.type = kind

    def model_dump_json(self) -> str:
        return f'{{"type": "{self.type}"}}'


class _FakeLoop:
    """Yields a fixed event stream in place of the real ReAct loop, and records the kwargs
    run_turn drove it with (so the test can assert the window was passed through)."""

    seen_kwargs: dict = {}

    def __init__(self, *_a: object, **_k: object) -> None:
        pass

    async def run_stream(self, **kwargs: object):  # type: ignore[no-untyped-def]
        _FakeLoop.seen_kwargs = kwargs
        yield _Ev("text_delta")
        # A real UsageEvent so run_turn's isinstance check captures the fill.
        yield UsageEvent(input_tokens=1200, output_tokens=300, context_window=5000)
        yield _Ev("done")


class _FakeAcc:
    """A render accumulator that just records what it was fed, so the test doesn't couple to
    real event shapes."""

    def __init__(self) -> None:
        self.fed: list = []

    def feed(self, ev: object) -> None:
        self.fed.append(ev)

    answer_text = "hello"
    stop_reason = "stop"
    reasoning_text = ""

    def tool_steps(self) -> list[dict]:
        return []


async def _effort(_key: str) -> str:
    return "medium"


async def _window(_key: str) -> int:
    return 5000


def _router() -> SimpleNamespace:
    return SimpleNamespace(effective_reasoning_effort=_effort, context_window=_window)


@pytest.mark.asyncio
async def test_run_turn_streams_events_to_sink_and_uses_supplied_acc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `acc` + `on_event` supplied (the continuation's streaming path): every loop event
    is fed to the CALLER's accumulator (so it can serve as the reattach snapshot) AND handed
    to the sink (so it can emit an SSE frame onto the live-turn broker), in order."""
    monkeypatch.setattr(runner_mod, "AgentLoop", _FakeLoop)
    executor = LoopTurnExecutor(router=_router(), registry=object())  # type: ignore[arg-type]

    acc = _FakeAcc()
    seen: list = []
    executed = await executor.run_turn(
        profile=agent_for("jerv"),
        read_ctx=OWNER,
        read_scopes=(),
        conversation=[],
        timezone=None,
        recorder=object(),
        agent_session_id="sess-1",
        acc=acc,  # type: ignore[arg-type]
        on_event=seen.append,
    )

    # The sink and the caller's acc both saw every event (text_delta, usage, done), in order.
    assert [getattr(e, "type", None) for e in seen] == ["text_delta", "usage", "done"]
    assert [getattr(e, "type", None) for e in acc.fed] == ["text_delta", "usage", "done"]
    # The result is shaped from the SAME accumulator the caller passed.
    assert executed.result.text == "hello"
    assert executed.result.stop_reason == "stop"


@pytest.mark.asyncio
async def test_run_turn_passes_the_window_and_returns_the_context_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The meter fix (Gap #1/#2): run_turn resolves the context window and PASSES it into
    run_stream — which is what makes the loop emit UsageEvents on this (continuation/task) path
    — then returns the fullest step's fill so the caller can persist the meter seed. Without the
    window, the loop suppresses every UsageEvent and a reattached client's meter stays frozen."""
    monkeypatch.setattr(runner_mod, "AgentLoop", _FakeLoop)
    executor = LoopTurnExecutor(router=_router(), registry=object())  # type: ignore[arg-type]

    executed = await executor.run_turn(
        profile=agent_for("jerv"),
        read_ctx=OWNER,
        read_scopes=(),
        conversation=[],
        timezone=None,
        recorder=object(),
        agent_session_id="sess-1",
        acc=_FakeAcc(),  # type: ignore[arg-type]  # stub events don't fit the real accumulator
    )

    # The resolved window was threaded into the loop (so it emits UsageEvents at all)…
    assert _FakeLoop.seen_kwargs.get("context_window") == 5000
    # …and run_turn returned the fullest step's fill (input+output) + the window for the caller
    # (the continuation) to persist via record_context.
    assert executed.context_used == 1500
    assert executed.context_window == 5000


@pytest.mark.asyncio
async def test_root_tree_seeds_a_rooted_fan_so_deep_research_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`root_tree=True` (the plan-continuation path) seeds this turn as the ROOT of a sub-agent
    fan — a `TreeState` with a real budget — exactly as /chat does. That non-None `ctx.tree` is
    what lets a plan step call deep_research/deep_produce/spawn (all of which refuse when
    `ctx.tree is None`). Without it a plan whose steps ARE deep-research runs got 'only available
    in an interactive owner turn' on every step."""
    monkeypatch.setattr(runner_mod, "AgentLoop", _FakeLoop)
    executor = LoopTurnExecutor(router=_router(), registry=object())  # type: ignore[arg-type]

    await executor.run_turn(
        profile=agent_for("jerv"),
        read_ctx=OWNER,
        read_scopes=(),
        conversation=[],
        timezone=None,
        recorder=object(),
        agent_session_id="sess-1",
        acc=_FakeAcc(),  # type: ignore[arg-type]
        root_tree=True,
    )
    tree = _FakeLoop.seen_kwargs.get("tree")
    assert isinstance(tree, TreeState)
    assert tree.tree_budget > 0  # a real fan budget, sized off the turn's own per-turn cap


@pytest.mark.asyncio
async def test_no_root_tree_leaves_the_turn_treeless(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default (a headless scheduled task) passes NO tree, so `ctx.tree` stays None and the
    fan-out tools keep refusing — an autonomous cron turn does not silently gain deep research."""
    monkeypatch.setattr(runner_mod, "AgentLoop", _FakeLoop)
    executor = LoopTurnExecutor(router=_router(), registry=object())  # type: ignore[arg-type]

    await executor.run_turn(
        profile=agent_for("jerv"),
        read_ctx=OWNER,
        read_scopes=(),
        conversation=[],
        timezone=None,
        recorder=object(),
        agent_session_id="sess-1",
        acc=_FakeAcc(),  # type: ignore[arg-type]
    )
    assert _FakeLoop.seen_kwargs.get("tree") is None
