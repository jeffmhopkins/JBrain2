"""LoopTurnExecutor.run_turn's optional streaming sink (`acc` + `on_event`) — the seam that
lets a plan continuation stream its events onto a `_LiveTurn` broker while still folding the
durable transcript shape. AgentLoop is faked; no LLM, no DB."""

from types import SimpleNamespace

import pytest

import jbrain.tasks.runner as runner_mod
from jbrain.agent.agents import agent_for
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
    """Yields a fixed event stream in place of the real ReAct loop."""

    def __init__(self, *_a: object, **_k: object) -> None:
        pass

    async def run_stream(self, **_kwargs: object):  # type: ignore[no-untyped-def]
        for ev in (_Ev("text_delta"), _Ev("done")):
            yield ev


class _FakeAcc:
    """A render accumulator that just records what it was fed, so the test doesn't couple to
    real event shapes."""

    def __init__(self) -> None:
        self.fed: list[_Ev] = []

    def feed(self, ev: _Ev) -> None:
        self.fed.append(ev)

    answer_text = "hello"
    stop_reason = "stop"
    reasoning_text = ""

    def tool_steps(self) -> list[dict]:
        return []


async def _effort(_key: str) -> str:
    return "medium"


@pytest.mark.asyncio
async def test_run_turn_streams_events_to_sink_and_uses_supplied_acc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `acc` + `on_event` supplied (the continuation's streaming path): every loop event
    is fed to the CALLER's accumulator (so it can serve as the reattach snapshot) AND handed
    to the sink (so it can emit an SSE frame onto the live-turn broker), in order."""
    monkeypatch.setattr(runner_mod, "AgentLoop", _FakeLoop)
    router = SimpleNamespace(effective_reasoning_effort=_effort)
    executor = LoopTurnExecutor(router=router, registry=object())  # type: ignore[arg-type]

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

    assert [e.type for e in seen] == ["text_delta", "done"]  # the sink saw every event...
    assert [e.type for e in acc.fed] == ["text_delta", "done"]  # ...and so did the caller's acc
    # The result is shaped from the SAME accumulator the caller passed.
    assert executed.result.text == "hello"
    assert executed.result.stop_reason == "stop"
