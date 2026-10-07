"""jerv's history rebuilt from the transcript, tool calls and results included
(docs/plans/TOOL_RESULT_REPLAY_PLAN.md).

A chat turn used to see earlier turns as their prose only, so the evidence behind an answer
vanished when its turn ended — and the next turn guessed, or disowned an answer it had built
from real results. Here each earlier assistant turn is replayed as the model lived it: the
prose it wrote before each round of calls, the calls, their stored results, then the rest of
its prose.

Results are bounded by a token budget, newest first. A turn before the session's stored
`replay_floor_seq` keeps its calls but replays a one-line stub for each result, so jerv knows
what it did and can run it again. The boundary moves only forward and in steps (over the high
water mark it advances whole turns until the kept results are under the low one), so the replay
is byte-identical turn over turn and the engine's prefix cache holds it. Everything is computed
from stored rows with a fixed character-per-token ratio — never from a calibrated estimate that
drifts, which would move the boundary on its own.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from jbrain.agent.attachment_content import decorated_history_text
from jbrain.agent.transcript_store import TurnRecord
from jbrain.llm import (
    AssistantMessage,
    LlmMessage,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)

# Fixed, so the boundary depends on the stored text alone.
CHARS_PER_TOKEN = 4
# The owner's budget for replayed results (2026-10-06), and the level a move brings them back to.
REPLAY_BUDGET_TOKENS = 64 * 1024
REPLAY_LOW_WATER_TOKENS = 48 * 1024
# One huge page must not spend the whole budget.
MAX_RESULT_CHARS = 16_000
# Tools whose output is never replayed in full, whatever the budget. None yet: the session's
# own RLS read is the firewall for a result as it is for the prose around it.
NEVER_REPLAYED: frozenset[str] = frozenset()

STUB = "[result not shown — older than this chat's replay budget; call the tool again to see it]"
_CUT = "\n[result cut at {n:,} characters — call the tool again to read the rest]"


@dataclass(frozen=True)
class Entry:
    """One earlier turn: its role, the text the anchor matcher keys on (a user turn exactly as
    the PWA's history entry spells it), and the messages the model receives for it."""

    role: Literal["user", "assistant"]
    text: str
    messages: tuple[LlmMessage, ...]


def _replayable(step: dict[str, Any]) -> bool:
    return bool(step.get("id")) and bool(step.get("name"))


def result_text(step: dict[str, Any]) -> str:
    summary = str(step.get("summary") or "")
    if len(summary) > MAX_RESULT_CHARS:
        return summary[:MAX_RESULT_CHARS] + _CUT.format(n=MAX_RESULT_CHARS)
    return summary


def _turn_chars(turn: TurnRecord) -> int:
    return sum(
        len(result_text(s))
        for s in turn.tools
        if _replayable(s) and s.get("name") not in NEVER_REPLAYED
    )


def advance_floor(turns: Sequence[TurnRecord], floor: int) -> int:
    """The boundary this render uses: `floor`, moved forward by whole turns when the results
    it keeps exceed the budget, until they are under the low water mark. The newest turn with
    results is never stubbed — it is what a follow-up asks about."""
    kept = [t for t in turns if t.role == "assistant" and t.seq >= floor and _turn_chars(t)]
    total = sum(_turn_chars(t) for t in kept)
    if total <= REPLAY_BUDGET_TOKENS * CHARS_PER_TOKEN:
        return floor
    for turn in kept[:-1]:
        total -= _turn_chars(turn)
        floor = turn.seq + 1
        if total <= REPLAY_LOW_WATER_TOKENS * CHARS_PER_TOKEN:
            break
    return floor


def _assistant_messages(turn: TurnRecord, *, full: bool) -> tuple[LlmMessage, ...]:
    steps = [s for s in turn.tools if _replayable(s)]
    content = turn.content
    out: list[LlmMessage] = []
    at = 0
    i = 0
    while i < len(steps):
        # One round: the calls made at the same point in the prose.
        offset = min(max(int(steps[i].get("text_offset") or 0), at), len(content))
        round_steps = [steps[i]]
        i += 1
        while i < len(steps) and int(steps[i].get("text_offset") or 0) <= offset:
            round_steps.append(steps[i])
            i += 1
        out.append(
            AssistantMessage(
                text=content[at:offset],
                tool_calls=tuple(
                    ToolCall(
                        id=str(s["id"]), name=str(s["name"]), arguments=dict(s.get("args") or {})
                    )
                    for s in round_steps
                ),
            )
        )
        out.append(
            ToolResultMessage(
                results=tuple(
                    ToolResult(
                        tool_call_id=str(s["id"]),
                        content=(
                            result_text(s) if full and s.get("name") not in NEVER_REPLAYED else STUB
                        ),
                        is_error=s.get("ok") is False,
                    )
                    for s in round_steps
                )
            )
        )
        at = offset
    rest = content[at:]
    if rest or not out:
        out.append(AssistantMessage(text=rest))
    return tuple(out)


def _user_text(turn: TurnRecord) -> str:
    media = [
        a
        for a in turn.attachments
        if a.media_type.startswith("image/") or a.media_type.startswith("video/")
    ]
    return decorated_history_text(turn.content, media) if media else turn.content


def build(turns: Sequence[TurnRecord], floor: int) -> list[Entry]:
    """Every earlier turn, oldest first. Results of turns before `floor` are stubbed."""
    entries: list[Entry] = []
    for turn in turns:
        if turn.role == "user":
            text = _user_text(turn)
            entries.append(Entry("user", text, (UserMessage(text=text),)))
        elif turn.role == "assistant":
            messages = _assistant_messages(turn, full=turn.seq >= floor)
            entries.append(Entry("assistant", turn.content, messages))
    return entries
