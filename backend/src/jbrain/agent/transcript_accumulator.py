"""Folding a turn's ChatEvent stream into the persisted transcript shape.

Both the live /chat turn (api/agent.py) and the headless task turn (tasks/runner.py)
persist the SAME assistant-turn record — the streamed answer, the reasoning trace,
and the tool "Worked" steps with their surfaced sources / proposals / entities /
views — so a session reopened from either path replays identically. This is the one
place that folds the event stream into that shape, so the two callers cannot drift
(a scheduled task once recorded no tools and no reasoning because it shaped its own,
emptier record — see tasks/runner.py).
"""

from dataclasses import dataclass, field
from typing import Any

from jbrain.agent.contracts import ChatEvent


@dataclass
class TranscriptAccumulator:
    """Accumulates the durable parts of one turn from its ChatEvent stream.

    Answer and reasoning are kept as parts (not joined) so a caller that already
    threads a `list[str]` of answer chunks — the /chat recorders do — keeps its
    shape. Display-only events (progress, usage, verdict, general-knowledge) are
    deliberately not folded in: they are ephemeral and never persisted."""

    answer: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    stop_reason: str = "error"
    done: bool = False
    _steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    def feed(self, event: ChatEvent) -> None:
        if event.type == "text_delta":
            self.answer.append(event.text)
        elif event.type == "reasoning_delta":
            self.reasoning.append(event.text)
        elif event.type == "tool_call":
            self._steps[event.id] = {
                "id": event.id,
                "name": event.name,
                "ok": None,
                "sources": [],
                # The length of the answer text streamed BEFORE this call — the point
                # the turn's prose splits around the tool. The PWA uses it to render an
                # image turn as preamble → image → reply, and persisting it replays the
                # same split on reopen.
                "text_offset": len("".join(self.answer)),
                # The call's arguments, so an expanded step replays what it ran on
                # reopen — the web tools' url/query especially, which carry no
                # NoteSource to stand in for them. Empty args stay omitted (noise).
                **({"args": event.arguments} if event.arguments else {}),
            }
            self._order.append(event.id)
        elif event.type == "tool_result":
            step = self._steps.get(event.tool_call_id)
            if step is not None:
                step["ok"] = event.ok
                # The verbatim result text, so a step's result rung replays on reopen —
                # for a sourceless tool (the web tools) it is the only content shown.
                step["summary"] = event.summary
                step["sources"] = [s.model_dump() for s in event.sources]
                # Staged-proposal and resolved-entity chips, so the bubble replays in
                # full on reopen (not just sources).
                if event.proposal is not None:
                    step["proposal"] = event.proposal.model_dump()
                if event.entities:
                    step["entities"] = [e.model_dump() for e in event.entities]
                # Web citation sources (jerv) — persisted so the favicon chips and
                # their [^n] targets replay on reopen.
                if event.web_sources:
                    step["web_sources"] = [s.model_dump() for s in event.web_sources]
        elif event.type == "tool_view":
            # The rich view (e.g. a list_card) rides its tool step so the bubble's
            # tool-result views replay on reopen.
            step = self._steps.get(event.tool_call_id)
            if step is not None:
                step["view"] = event.view.model_dump()
        elif event.type == "done":
            self.stop_reason = event.stop_reason
            self.done = True

    @property
    def answer_text(self) -> str:
        return "".join(self.answer)

    @property
    def reasoning_text(self) -> str:
        return "".join(self.reasoning)

    def tool_steps(self) -> list[dict[str, Any]]:
        """The assistant turn's ordered "Worked" steps, ready for the transcript."""
        return [self._steps[i] for i in self._order]
