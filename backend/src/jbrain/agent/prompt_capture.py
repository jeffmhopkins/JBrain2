"""The prompt a run actually sent to the model, kept in memory for the vitals detail.

Until now this was the one thing the debug surface could not show: the assembled prompt
is built per model call and was discarded the moment the call returned, so the screen
said "not stored" rather than inventing it. This records it.

IN MEMORY, AND DELIBERATELY NOT IN THE DATABASE. An assembled agent prompt is a verbatim
copy of everything the turn retrieved to answer — notes, wiki text, tool results, and
whatever the retrieval pulled from the health, finance and location domains. Writing it
to a table would take data whose protection is row-level security in Postgres and
duplicate it into a new artifact that every backup then carries, with its own retention
and its own blast radius, to answer a question about what the box is doing *right now*.
A process-lifetime ring answers that question and forgets, which is the honest trade for
a debug read-out. A restart empties it and the surface says so, exactly as it already
does for a run whose call was never stamped.

Two bounds keep it from growing without limit: only the most recent runs are held, and
each prompt is clipped. The clip is visible in the payload (`truncated`) so a long
prompt reads as clipped rather than as a short one.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

# How many runs keep their prompt. A deep-research fan is a parent plus its children, so
# this holds a couple of full fans — enough to open the screen and drill into any row
# that is still on it.
MAX_RUNS = 24

# Per-prompt clip. A full context can run to hundreds of kilobytes; this keeps the head,
# which carries the system prompt and the opening of the conversation — the part that
# answers "what was it actually asked".
MAX_CHARS = 200_000


@dataclass(frozen=True, slots=True)
class CapturedPrompt:
    """One model call's assembled input, as it went to the provider."""

    system: str
    messages: list[dict[str, Any]]
    tools: list[str]
    # Which round of the turn's ReAct loop this was, counting from 1. A turn sends
    # several; the latest is kept, because it carries every earlier tool result and so
    # is the fullest account of what the model was looking at.
    round_index: int
    truncated: bool = False
    system_chars: int = 0
    message_chars: int = 0


@dataclass
class _Store:
    """Most-recent-first, bounded. Not thread-safe by design: every writer is on the
    API's single event loop."""

    by_run: OrderedDict[str, CapturedPrompt] = field(default_factory=OrderedDict)
    rounds: dict[str, int] = field(default_factory=dict)

    def record(self, run_id: str, prompt: CapturedPrompt) -> None:
        self.by_run.pop(run_id, None)
        self.by_run[run_id] = prompt
        while len(self.by_run) > MAX_RUNS:
            evicted, _ = self.by_run.popitem(last=False)
            self.rounds.pop(evicted, None)

    def get(self, run_id: str) -> CapturedPrompt | None:
        return self.by_run.get(run_id)


_store = _Store()


def _clip(text: str, budget: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= budget else (text[:budget], True)


def record_prompt(
    run_id: str | None,
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[str],
) -> None:
    """Keep this call's assembled prompt as the run's latest.

    Best-effort and cheap: a shallow copy of already-materialised strings, no I/O. A
    turn without a run id (a headless probe, a test) records nothing."""
    if not run_id:
        return
    round_index = _store.rounds.get(run_id, 0) + 1
    _store.rounds[run_id] = round_index

    system_text, system_clipped = _clip(system, MAX_CHARS)
    budget = max(0, MAX_CHARS - len(system_text))
    kept: list[dict[str, Any]] = []
    clipped = system_clipped
    for message in messages:
        content = str(message.get("content") or "")
        if budget <= 0:
            clipped = True
            break
        text, was_clipped = _clip(content, budget)
        clipped = clipped or was_clipped
        budget -= len(text)
        kept.append({"role": str(message.get("role") or ""), "content": text})

    _store.record(
        run_id,
        CapturedPrompt(
            system=system_text,
            messages=kept,
            tools=tools,
            round_index=round_index,
            truncated=clipped or len(kept) < len(messages),
            system_chars=len(system),
            message_chars=sum(len(str(m.get("content") or "")) for m in messages),
        ),
    )


def prompt_for(run_id: str) -> CapturedPrompt | None:
    """The run's latest assembled prompt, or None — it predates this process, was
    evicted, or the run never reached a model call."""
    return _store.get(run_id)


def forget(run_id: str) -> None:
    """Drop a run's captured prompt. Used by tests; the ring evicts on its own."""
    _store.by_run.pop(run_id, None)
    _store.rounds.pop(run_id, None)
