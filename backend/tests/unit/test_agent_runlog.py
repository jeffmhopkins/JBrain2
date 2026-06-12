"""The bound recorder forwards each loop step to its run + context (no DB)."""

from jbrain.agent.runlog import BoundRecorder
from jbrain.db.session import SessionContext


async def test_bound_recorder_forwards_step_to_log() -> None:
    calls: list[tuple] = []

    class FakeLog:
        async def step(self, ctx, run_id, *, idx, kind, name, ok, cost_tokens) -> None:  # noqa: ANN001
            calls.append((run_id, idx, kind, name, ok, cost_tokens))

    recorder = BoundRecorder(FakeLog(), SessionContext(principal_kind="owner"), "run-1")  # type: ignore[arg-type]
    await recorder.step(idx=0, kind="model", name="converse", ok=True, cost_tokens=5)
    await recorder.step(idx=1, kind="tool", name="search", ok=False, cost_tokens=0)

    assert calls == [
        ("run-1", 0, "model", "converse", True, 5),
        ("run-1", 1, "tool", "search", False, 0),
    ]
