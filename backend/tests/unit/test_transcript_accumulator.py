"""The transcript accumulator folds a turn's ChatEvent stream into the persisted
shape. These pin the per-tool offsets it records — the answer-text split point
(text_offset) and the reasoning-trace split point (reasoning_offset) — so a reopened
session replays the same prose split and the same in-thinking tool interleave."""

from jbrain.agent.contracts import (
    DoneEvent,
    ReasoningDelta,
    ReasoningReclassify,
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


def test_reclassify_moves_the_leaked_answer_tail_into_reasoning() -> None:
    # On the local (harmony) route a tool-call round's leaked analysis streams live into the
    # answer, then a ReasoningReclassify names it for relocation. The accumulator must move it
    # out of the answer and into the reasoning trace so the persisted transcript matches the
    # pre-streaming buffered classification — and the subsequent tool's text_offset is 0
    # (the answer is empty again), while its reasoning_offset covers the reclassified analysis.
    acc = TranscriptAccumulator()
    acc.feed(TextDelta(text="analysis: now call search."))
    acc.feed(ReasoningReclassify(text="analysis: now call search."))
    acc.feed(ToolCallEvent(id="c1", name="search", arguments={"q": "x"}))
    acc.feed(ToolResultEvent(tool_call_id="c1", ok=True, summary="found"))
    acc.feed(TextDelta(text="the real answer"))
    acc.feed(DoneEvent(stop_reason="end_turn"))

    assert acc.answer_text == "the real answer"
    assert acc.reasoning_text == "analysis: now call search."
    step = acc.tool_steps()[0]
    assert step["text_offset"] == 0
    assert step["reasoning_offset"] == len("analysis: now call search.")


def test_reclassify_only_strips_a_matching_tail() -> None:
    # Defensive: if the reclassify text isn't the answer's tail (an out-of-order replay), the
    # answer is left intact and the text is still recorded as reasoning — never a bad slice.
    acc = TranscriptAccumulator()
    acc.feed(TextDelta(text="kept answer"))
    acc.feed(ReasoningReclassify(text="unrelated"))

    assert acc.answer_text == "kept answer"
    assert acc.reasoning_text == "unrelated"


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


def test_render_snapshot_is_the_live_render_without_the_interrupted_coercion() -> None:
    # The reattach snapshot (a reloaded PWA seeding its bubble from the turn's render so far)
    # must NOT mark an in-flight tool as failed the way `tool_steps` does at settle: the turn
    # is still running, so the step stays `ok=None` and replays as a live spinner, not an
    # error. It carries the answer/reasoning accumulated so far in the transcript's shape.
    acc = TranscriptAccumulator()
    acc.feed(ReasoningDelta(text="planning the fan"))
    acc.feed(TextDelta(text="partial answer"))
    acc.feed(ToolCallEvent(id="c1", name="deep_research", arguments={"breadth": 5}))
    # No tool_result yet — the fan is still running when the owner reloads.

    snap = acc.render_snapshot()
    assert snap["role"] == "assistant"
    assert snap["content"] == "partial answer"
    assert snap["reasoning"] == "planning the fan"
    assert snap["tools"][0]["name"] == "deep_research"
    assert snap["tools"][0]["ok"] is None


def test_render_snapshot_does_not_mutate_the_live_accumulator() -> None:
    # The snapshot is read off a LIVE accumulator the driving task keeps feeding, so it must
    # be non-mutating: serializing it can't flip an unsettled step to failed (which would then
    # persist wrongly at the real settle) — the step dict is copied.
    acc = TranscriptAccumulator()
    acc.feed(ToolCallEvent(id="c1", name="deep_research", arguments={}))

    acc.render_snapshot()
    # The live step is untouched — still unsettled — so when the tool DOES return, the settle
    # path records its real result rather than a stuck "(interrupted)".
    assert acc._steps["c1"]["ok"] is None
    acc.feed(ToolResultEvent(tool_call_id="c1", ok=True, summary="done"))
    assert acc.tool_steps()[0]["ok"] is True
    assert acc.tool_steps()[0]["summary"] == "done"
