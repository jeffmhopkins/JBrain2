"""jerv's history rebuilt from the transcript, tool calls and results included
(docs/plans/TOOL_RESULT_REPLAY_PLAN.md).

A chat turn used to see earlier turns as their prose only, so the evidence behind an answer
vanished when its turn ended — and the next turn guessed, or disowned an answer it had built
from real results. Here each earlier assistant turn is replayed as the model lived it: the
prose it wrote before each round of calls, the calls, their stored results, then the rest of
its prose.

On a local route (`exact`) a turn whose rounds were recorded (`agent_turns.wire`) replays
EXACTLY as the model was sent it: each round's own text, thinking and model, its calls with the
arguments as they were serialized, results with any model-only suffix, and the turn's own
user-side messages (its volatile blocks included). Anything less and the next turn's prompt is
not an extension of the last one — it diverges at the previous turn's first step, and a hybrid
model, which reuses its cache only from a checkpoint before the divergence, re-reads the whole
chat (docs/reference/PROMPT_CACHE.md, "A follow-up is an exact extension").

Replayed bulk — results, and on the exact path thinking and the turns' own blocks — is bounded
by a token budget, newest first. A turn before the session's stored
`replay_floor_seq` keeps its calls but replays a one-line stub for each result, so jerv knows
what it did and can run it again. The boundary moves only forward and in steps (over the high
water mark it advances whole turns until the kept results are under the low one), so the replay
is byte-identical turn over turn and the engine's prefix cache holds it. Everything is computed
from stored rows with a fixed character-per-token ratio — never from a calibrated estimate that
drifts, which would move the boundary on its own.
"""

import json
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
# On the exact path it bounds everything replayed beyond the prose: thinking and each turn's own
# blocks count against it too, so the chat's growth stays inside the same ceiling.
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
    the PWA's history entry spells it), and the messages the model receives for it. `tail`
    follows the turn's image anchor, where one is placed: a replayed user turn's own blocks and
    message came after its live anchor, so they must again."""

    role: Literal["user", "assistant"]
    text: str
    messages: tuple[LlmMessage, ...]
    tail: tuple[LlmMessage, ...] = ()


def _replayable(step: dict[str, Any]) -> bool:
    return bool(step.get("id")) and bool(step.get("name"))


def result_text(step: dict[str, Any]) -> str:
    summary = str(step.get("summary") or "")
    if len(summary) > MAX_RESULT_CHARS:
        return summary[:MAX_RESULT_CHARS] + _CUT.format(n=MAX_RESULT_CHARS)
    return summary


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _wire(turn: TurnRecord) -> dict[str, Any] | None:
    """The turn's recorded rounds, or None when there is no usable record (a turn stored before
    it existed, or a malformed one): the turn then replays from its prose. Every call must
    resolve to a stored step, whose result the replay sends."""
    wire = turn.wire
    if turn.role != "assistant" or not isinstance(wire, dict) or wire.get("v") != 1:
        return None
    if wire.get("full"):
        # The turn ended at (or near) the slot's ceiling: replayed whole it would overflow
        # every follow-up. The prose replay cuts its results — one re-read, and the chat lives.
        return None
    rounds = wire.get("rounds")
    if not isinstance(rounds, list):
        return None
    steps = {str(s.get("id")) for s in turn.tools if _replayable(s)}
    for r in rounds:
        if not isinstance(r, dict) or not isinstance(r.get("calls"), list):
            return None
        if any(_str(r.get(k)) is None for k in ("text", "reasoning", "model")):
            return None
        for call in r["calls"]:
            if not isinstance(call, dict) or _str(call.get("id")) not in steps:
                return None
            try:
                if not isinstance(json.loads(str(call.get("arguments"))), dict):
                    return None
            except ValueError:
                return None
    final = wire.get("final")
    if final is not None and (
        not isinstance(final, dict)
        or any(_str(final.get(k)) is None for k in ("text", "reasoning", "model"))
    ):
        return None
    return wire


def _input(wire: dict[str, Any] | None) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """The turn's own user-side messages as sent: (before its image anchor, after it)."""
    record = wire.get("input") if wire is not None else None
    if not isinstance(record, dict):
        return None
    head, tail = record.get("head"), record.get("tail")
    if not isinstance(head, list) or not isinstance(tail, list) or not tail:
        return None
    if not all(isinstance(t, str) for t in (*head, *tail)):
        return None
    return tuple(head), tuple(tail)


def _turn_chars(turn: TurnRecord, *, exact: bool = False) -> int:
    wire = _wire(turn) if exact else None
    if wire is None:
        return sum(
            len(result_text(s))
            for s in turn.tools
            if _replayable(s) and s.get("name") not in NEVER_REPLAYED
        )
    suffixes = wire.get("suffixes") or {}
    rounds = wire["rounds"]
    results = sum(
        len(str(s.get("summary") or "")) + len(str(suffixes.get(s.get("id"), "")))
        for s in turn.tools
        if _replayable(s) and s.get("name") not in NEVER_REPLAYED
    )
    thinking = sum(len(r["reasoning"]) for r in rounds)
    thinking += len(wire["final"]["reasoning"]) if wire.get("final") else 0
    own = _input(wire)
    blocks = sum(len(t) for t in (*own[0], *own[1])) if own is not None else 0
    return results + thinking + blocks


def advance_floor(turns: Sequence[TurnRecord], floor: int, *, exact: bool = False) -> int:
    """The boundary this render uses: `floor`, moved forward by whole turns when the bulk
    it keeps exceeds the budget, until it is under the low water mark. The newest turn with
    tool RESULTS is never compacted, nor anything after it — it is what a follow-up asks about.
    Keyed on results, not on bulk: on the exact path every recorded turn has some (its `now`
    block, its thinking), and protecting merely the newest would let a "thanks" after a big
    research turn stub that turn for good. Moving it is the ONE place a render deliberately
    stops extending the last one: every turn it passes is re-rendered compact, once, and the
    engine re-reads from the oldest of them."""
    kept = [
        t for t in turns if t.role == "assistant" and t.seq >= floor and _turn_chars(t, exact=exact)
    ]
    total = sum(_turn_chars(t, exact=exact) for t in kept)
    if total <= REPLAY_BUDGET_TOKENS * CHARS_PER_TOKEN:
        return floor
    with_results = [t.seq for t in kept if _turn_chars(t)]
    protected = with_results[-1] if with_results else kept[-1].seq
    for turn in kept:
        if turn.seq >= protected:
            break
        total -= _turn_chars(turn, exact=exact)
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


def _exact_messages(turn: TurnRecord, wire: dict[str, Any]) -> tuple[LlmMessage, ...]:
    """The turn's rounds exactly as the model was sent them, then its final answer."""
    steps = {str(s["id"]): s for s in turn.tools if _replayable(s)}
    suffixes = wire.get("suffixes") or {}
    out: list[LlmMessage] = []
    for r in wire["rounds"]:
        calls = tuple(
            ToolCall(id=c["id"], name=c["name"], arguments=json.loads(c["arguments"]))
            for c in r["calls"]
        )
        out.append(
            AssistantMessage(
                text=r["text"],
                tool_calls=calls,
                reasoning=r["reasoning"],
                reasoning_model=r["model"],
            )
        )
        if not calls:
            continue
        out.append(
            ToolResultMessage(
                results=tuple(
                    ToolResult(
                        tool_call_id=c.id,
                        content=(
                            STUB
                            if c.name in NEVER_REPLAYED
                            else str(steps[c.id].get("summary") or "") + str(suffixes.get(c.id, ""))
                        ),
                        is_error=steps[c.id].get("ok") is False,
                    )
                    for c in calls
                )
            )
        )
    final = wire.get("final")
    if final is not None:
        out.append(
            AssistantMessage(
                text=final["text"], reasoning=final["reasoning"], reasoning_model=final["model"]
            )
        )
        return tuple(out)
    # No final round recorded (the turn stopped on a tool, a cap or an error): the prose after
    # the rounds, which no earlier prompt held, so any spelling of it extends the last one.
    said = "".join(r["text"] for r in wire["rounds"])
    if turn.content.startswith(said):
        rest = turn.content[len(said) :]
    else:
        # A round's text was moved into the thinking on the way to the transcript (gpt-oss's
        # leaked analysis), so the prose no longer starts with it: the answer is what was
        # streamed after the last call, as the prose replay reads it.
        called = {c["id"] for r in wire["rounds"] for c in r["calls"]}
        offsets = [int(s.get("text_offset") or 0) for s in turn.tools if s.get("id") in called]
        rest = turn.content[max(offsets, default=0) :]
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


def _users(texts: Sequence[str]) -> tuple[LlmMessage, ...]:
    return tuple(UserMessage(text=t) for t in texts)


def build(turns: Sequence[TurnRecord], floor: int, *, exact: bool = False) -> list[Entry]:
    """Every earlier turn, oldest first. Results of turns before `floor` are stubbed.

    `exact` (a local route) replays each recorded turn at or after `floor` exactly as it was
    sent — its own user-side messages included, on the user turn before it (or on itself when
    no user turn was stored, the deferred auto-resume). Off, every turn replays from its prose,
    the shape a cloud provider has always been sent."""
    entries: list[Entry] = []
    for index, turn in enumerate(turns):
        nxt = turns[index + 1] if index + 1 < len(turns) else None
        if turn.role == "user":
            text = _user_text(turn)
            own = (
                _input(_wire(nxt))
                if exact and nxt is not None and nxt.role == "assistant" and nxt.seq >= floor
                else None
            )
            if own is None:
                entries.append(Entry("user", text, (UserMessage(text=text),)))
            else:
                entries.append(Entry("user", text, _users(own[0]), _users(own[1])))
        elif turn.role == "assistant":
            wire = _wire(turn) if exact and turn.seq >= floor else None
            if wire is None:
                messages = _assistant_messages(turn, full=turn.seq >= floor)
            else:
                messages = _exact_messages(turn, wire)
                own = _input(wire)
                if own is not None and (index == 0 or turns[index - 1].role != "user"):
                    messages = (*_users(own[0]), *_users(own[1]), *messages)
            entries.append(Entry("assistant", turn.content, messages))
    return entries
