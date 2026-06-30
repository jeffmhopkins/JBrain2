"""The transcript accumulator folds a turn's ChatEvent stream into the persisted
shape. These pin the per-tool offsets it records — the answer-text split point
(text_offset) and the reasoning-trace split point (reasoning_offset) — so a reopened
session replays the same prose split and the same in-thinking tool interleave."""

from jbrain.agent.contracts import (
    DoneEvent,
    ReasoningDelta,
    TextDelta,
    ToolCallEvent,
    ToolResultEvent,
)
from jbrain.agent.transcript_accumulator import TranscriptAccumulator


def test_records_text_and_reasoning_offsets_at_the_tool_call() -> None:
    # A ReAct step reasons, calls a tool, then the next step answers. The step records
    # how much answer text AND how much reasoning had streamed before the call, so the
    # PWA can split the prose around the tool and place the tool inside the thinking.
    acc = TranscriptAccumulator()
    acc.feed(TextDelta(text="working"))
    acc.feed(ReasoningDelta(text="let me think"))
    acc.feed(ToolCallEvent(id="c1", name="search", arguments={"q": "x"}))
    acc.feed(ToolResultEvent(tool_call_id="c1", ok=True, summary="found"))
    acc.feed(TextDelta(text=" — done"))
    acc.feed(DoneEvent(stop_reason="end_turn"))

    step = acc.tool_steps()[0]
    assert step["text_offset"] == len("working")
    assert step["reasoning_offset"] == len("let me think")


def test_unsettled_tool_persists_as_interrupted_not_null() -> None:
    # A turn cut before a tool returned (a Stop / disconnect / timeout mid-spawn) leaves
    # the step's `ok` null. Persisting it that way replays as a perpetual in-flight
    # spinner on reopen, so it settles to a failed/interrupted step instead.
    acc = TranscriptAccumulator()
    acc.feed(ToolCallEvent(id="c1", name="spawn_subagent", arguments={"tasks": []}))
    # No tool_result arrives — the turn is cut here.
    acc.feed(DoneEvent(stop_reason="disconnected"))

    step = acc.tool_steps()[0]
    assert step["ok"] is False
    assert step["summary"] == "(interrupted)"


def test_settled_tool_keeps_its_real_result() -> None:
    # A tool that DID return is untouched — the interrupted coercion only fires on a null.
    acc = TranscriptAccumulator()
    acc.feed(ToolCallEvent(id="c1", name="search", arguments={}))
    acc.feed(ToolResultEvent(tool_call_id="c1", ok=True, summary="found 3"))
    acc.feed(DoneEvent(stop_reason="end_turn"))

    step = acc.tool_steps()[0]
    assert step["ok"] is True
    assert step["summary"] == "found 3"


def test_reasoning_offset_tracks_interleaved_steps() -> None:
    # Reasoning accumulates across ReAct steps; each tool's reasoning_offset is the
    # reasoning length at its own call, so two tools split the trace where each ran.
    acc = TranscriptAccumulator()
    acc.feed(ReasoningDelta(text="first"))
    acc.feed(ToolCallEvent(id="c1", name="search", arguments={}))
    acc.feed(ToolResultEvent(tool_call_id="c1", ok=True, summary=""))
    acc.feed(ReasoningDelta(text=" then more"))
    acc.feed(ToolCallEvent(id="c2", name="read_note", arguments={}))
    acc.feed(ToolResultEvent(tool_call_id="c2", ok=True, summary=""))
    acc.feed(DoneEvent(stop_reason="end_turn"))

    steps = acc.tool_steps()
    assert steps[0]["reasoning_offset"] == len("first")
    assert steps[1]["reasoning_offset"] == len("first then more")
