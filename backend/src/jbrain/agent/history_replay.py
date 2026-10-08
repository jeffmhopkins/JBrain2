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
is byte-identical turn over turn and the engine's prefix cache holds it. On the exact path the
marks are fractions of the whole prompt's estimated size against the context window, far apart:
every move costs one re-read of nearly the whole chat, so it should come rarely and go deep.
Everything is computed from stored rows with a fixed character-per-token ratio — never from a
calibrated estimate that drifts, which would move the boundary on its own.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from jbrain.agent.attachment_content import decorated_history_text
from jbrain.agent.loop import TURN_MAX_TOKENS
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
# The exact (local) path compacts rarely and deeply instead (owner, 2026-10-08). A move there
# re-reads nearly the whole chat — measured: 99,160 tokens from zero, ~5½ minutes — and the
# 64k/48k marks, only 16k apart, had a page-heavy chat paying that every couple of research
# turns. Measured against the whole prompt and the window instead, in REAL tokens: compact when
# the prompt reaches 80% of the window, down to 50% — one re-read per ~80k tokens of growth.
EXACT_COMPACT_AT = 0.80
EXACT_COMPACT_TO = 0.50
# The window when the caller cannot name one: the local chat slot's cap.
EXACT_CONTEXT_WINDOW = 262_144
# What every prompt carries besides the history: the system prompt, the tool array and this
# turn's own `now`/context blocks. Measured at 43.6k tokens for jerv on the box (2026-10-08),
# rounded up. Only a chat with no real count yet (turns stored before the count was) leans on
# it; once a turn carries one, that count already holds all of this.
EXACT_OVERHEAD_TOKENS = 48 * 1024
# A chat's own characters-per-token, measured off its last call, is held to this range: code
# and JSON run near 2.5, prose near 4.5, and anything outside is a count gone wrong.
MEASURED_CHARS_PER_TOKEN = (2.5, 4.5)
# Room the newest research turn must leave for a model call to answer in — the loop's per-call
# output cap, a quarter of it. Keeping that turn whole with less than this is a render past the
# slot cap, which overflows the moment it is sent, so it yields too.
OUTPUT_ROOM_TOKENS = TURN_MAX_TOKENS // 4
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
    # The turn's stored results and cited URLs whatever `messages` replays — what the URL
    # provenance gate (agent/url_provenance.py) seeds from, so a site a compacted turn found
    # stays fetchable after its result is cut to a stub. Never sent to the model.
    provenance: tuple[str, ...] = ()


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


def _prose_chars(turn: TurnRecord) -> int:
    """What a turn replays whatever the floor: its prose, its calls' arguments, and a stub
    per result (counted on every turn — a few hundred characters over, never under)."""
    steps = [s for s in turn.tools if _replayable(s)] if turn.role == "assistant" else []
    return len(turn.content) + sum(len(json.dumps(s.get("args") or {})) + len(STUB) for s in steps)


def _measured(turn: TurnRecord) -> int | None:
    """The real size, in tokens, of the turn's last model call — its prompt plus what it wrote —
    as the engine reported it. None for a turn stored before the count was recorded."""
    wire = turn.wire
    usage = wire.get("usage") if turn.role == "assistant" and isinstance(wire, dict) else None
    if not isinstance(usage, dict):
        return None
    read, wrote = usage.get("input"), usage.get("output")
    if not isinstance(read, int) or not isinstance(wrote, int) or read <= 0:
        return None
    return read + max(wrote, 0)


def _unread_results(turn: TurnRecord) -> int:
    """Characters of results the turn's last call never read: its last round's, when the turn
    ended on calls rather than on an answer (a halt, a cut, a Stop)."""
    wire = turn.wire
    if not isinstance(wire, dict) or wire.get("final") is not None:
        return 0
    rounds = wire.get("rounds")
    last = rounds[-1] if isinstance(rounds, list) and rounds else None
    called = {c.get("id") for c in last.get("calls", [])} if isinstance(last, dict) else set()
    suffixes = wire.get("suffixes") or {}
    return sum(
        len(str(s.get("summary") or "")) + len(str(suffixes.get(s.get("id"), "")))
        for s in turn.tools
        if s.get("id") in called
    )


def _ended_full(turns: Sequence[TurnRecord]) -> bool:
    """The newest assistant turn overflowed, or ended within reach of the window (`full`)."""
    newest = next((t for t in reversed(turns) if t.role == "assistant"), None)
    wire = newest.wire if newest is not None else None
    return isinstance(wire, dict) and bool(wire.get("full"))


def advance_floor(
    turns: Sequence[TurnRecord],
    floor: int,
    *,
    exact: bool = False,
    context_window: int | None = None,
    overhead_tokens: int = EXACT_OVERHEAD_TOKENS,
    pending_chars: int = 0,
) -> int:
    """The boundary this render uses: `floor`, moved forward by whole turns when the bulk
    it keeps exceeds the budget, until it is under the low water mark. The newest turn with
    tool RESULTS is never compacted, nor anything after it — it is what a follow-up asks about.
    Keyed on results, not on bulk: on the exact path every recorded turn has some (its `now`
    block, its thinking), and protecting merely the newest would let a "thanks" after a big
    research turn stub that turn for good. Moving it is the ONE place a render deliberately
    stops extending the last one: every turn it passes is re-rendered compact, once, and the
    engine re-reads from the oldest of them.

    `exact` sizes the whole prompt against `context_window` instead (`_whole_prompt`), unless
    the window is too small for that to leave the history any room. `pending_chars` is the
    message this render adds, which no stored row holds yet."""
    kept = [
        t for t in turns if t.role == "assistant" and t.seq >= floor and _turn_chars(t, exact=exact)
    ]
    if not kept:
        return floor
    window = context_window or EXACT_CONTEXT_WINDOW
    if exact and overhead_tokens <= window * EXACT_COMPACT_TO:
        return _whole_prompt(turns, kept, floor, window, overhead_tokens, pending_chars)
    # The cloud path; and a small local window (a 32k or 64k model), whose fixed overhead alone
    # is past the 50% target — the whole-prompt rule would compact it on every turn.
    total = sum(_turn_chars(t, exact=exact) for t in kept)
    if total <= REPLAY_BUDGET_TOKENS * CHARS_PER_TOKEN:
        return floor
    protected = _protected(kept)
    for turn in kept:
        if turn.seq >= protected:
            break
        total -= _turn_chars(turn, exact=exact)
        floor = turn.seq + 1
        if total <= REPLAY_LOW_WATER_TOKENS * CHARS_PER_TOKEN:
            break
    return floor


def _protected(kept: Sequence[TurnRecord]) -> int:
    with_results = [t.seq for t in kept if _turn_chars(t)]
    return with_results[-1] if with_results else kept[-1].seq


def _whole_prompt(
    turns: Sequence[TurnRecord],
    kept: Sequence[TurnRecord],
    floor: int,
    window: int,
    overhead_tokens: int,
    pending_chars: int,
) -> int:
    """The exact path's rule, in real tokens. The newest turn that carries the engine's own
    count of its last call anchors the size; what came after it — later turns stored without a
    count, results that call never read, and this render's new message — is estimated from its
    characters at the chat's OWN ratio, measured off that same render, so a code- or JSON-heavy
    chat is not undercounted. A pasted document or an image on the new message is not in that
    estimate; everything already in the history is, by the count. A chat with no count at all
    falls back to the fixed overhead and 4 characters a token.

    Compacts when the prompt reaches `EXACT_COMPACT_AT` of the window — or regardless, when the
    newest turn ended `full` (it overflowed, or came within reach of the window) — down to
    `EXACT_COMPACT_TO`. The newest research turn yields only when keeping it would leave less
    than `OUTPUT_ROOM_TOKENS` of the window to answer in."""

    def bulk(t: TurnRecord) -> int:
        return _turn_chars(t, exact=True) if t.role == "assistant" and t.seq >= floor else 0

    anchor = next((i for i in range(len(turns) - 1, -1, -1) if _measured(turns[i])), None)
    if anchor is None:
        ratio = float(CHARS_PER_TOKEN)
        chars = overhead_tokens * CHARS_PER_TOKEN
        chars += sum(_prose_chars(t) + bulk(t) for t in turns) + pending_chars
        size = chars / ratio
    else:
        real = _measured(turns[anchor]) or 0
        unread = _unread_results(turns[anchor])
        rendered = overhead_tokens * CHARS_PER_TOKEN
        rendered += sum(_prose_chars(t) + bulk(t) for t in turns[: anchor + 1]) - unread
        low, high = MEASURED_CHARS_PER_TOKEN
        ratio = min(high, max(low, rendered / real))
        since = sum(_prose_chars(t) + bulk(t) for t in turns[anchor + 1 :])
        size = real + (unread + since + pending_chars) / ratio
    if size < window * EXACT_COMPACT_AT and not _ended_full(turns):
        return floor
    protected = _protected(kept)
    for turn in kept:
        if turn.seq >= protected and size <= window - OUTPUT_ROOM_TOKENS:
            break
        size -= _turn_chars(turn, exact=True) / ratio
        floor = turn.seq + 1
        if size <= window * EXACT_COMPACT_TO:
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


def _provenance(turn: TurnRecord) -> tuple[str, ...]:
    texts: list[str] = []
    for step in turn.tools:
        texts.append(str(step.get("summary") or ""))
        for source in step.get("web_sources") or ():
            if isinstance(source, dict):
                texts.append(str(source.get("url") or ""))
    return tuple(t for t in texts if t)


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
            entries.append(Entry("assistant", turn.content, messages, provenance=_provenance(turn)))
    return entries
