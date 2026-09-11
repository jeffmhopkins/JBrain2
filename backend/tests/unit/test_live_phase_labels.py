"""Roster gate for the PWA's LIVE PHASE line (frontend/src/agent/status.ts).

`test_tool_step_polish.py` gates a different map for a different strip: `toolSummary.ts`
is the Worked disclosure, the SETTLED steps, and its roster is every `.tool` sidecar.
This one gates `status.ts`'s `TOOL_LABELS`, the single line above the composer that says
what the agent is doing RIGHT NOW — and nothing gated it before, so the note
conversation's own verbs fell through to `{label: "Using", emphasis: name}` and a live
ingest pass read "Using resolve_entity" at the exact moment the owner was watching the
box read his note (AGENT_INGEST_REWRITE §3b I4).

**Scoped to the note-conversation tool sets on purpose, not to the whole roster.** The
sidecar roster is ~124 tools and this map carries a couple of dozen; asserting over all
of them would demand ~114 labels nobody has written and would land red, and a gate that
lands red on the day it ships is a gate that gets skipped. The three frozensets of
`agents.py` are already the enumerated closed lists this plan maintains, so the assertion
is small, meaningful, and green the day the labels land. `status.test.ts:130-132` still
blesses the generic fallback for an unmapped tool, and that is still right: a generic
"Using …" on some connector tool in a chat is a much smaller wrong than one on the note
thread.

Parsed with a regex over the literal map — the frontend file is the single source of
truth and this only keeps it honest against the sets."""

import re
from pathlib import Path

from jbrain.agent.agents import (
    NOTE_INGEST_ON_REPLY_TOOLS,
    NOTE_INGEST_THIRD_PARTY_TOOLS,
    NOTE_INGEST_UNATTENDED_TOOLS,
)

_REPO = Path(__file__).resolve().parents[3]
_STATUS_TS = _REPO / "frontend" / "src" / "agent" / "status.ts"


def _tool_labels() -> set[str]:
    src = _STATUS_TS.read_text(encoding="utf-8")
    start = "const TOOL_LABELS: Record<string, { label: string; emphasis?: string }> = {"
    begin = src.index(start) + len(start)
    body = src[begin : src.index("\n};", begin)]
    return set(re.findall(r"^\s*(\w+): \{", body, re.MULTILINE))


def _note_tools() -> set[str]:
    """Every verb a note conversation can run, on any of its three surfaces."""
    return set(
        NOTE_INGEST_UNATTENDED_TOOLS | NOTE_INGEST_ON_REPLY_TOOLS | NOTE_INGEST_THIRD_PARTY_TOOLS
    )


def test_every_note_conversation_verb_has_a_live_phase_label() -> None:
    labels = _tool_labels()
    missing = sorted(_note_tools() - labels)
    assert not missing, (
        "a live note pass would show the raw tool name on the status line above the "
        f"composer — add a TOOL_LABELS entry in {_STATUS_TS.relative_to(_REPO)}: {missing}"
    )


def test_the_gate_reads_a_map_that_is_actually_there() -> None:
    """The parse is the gate. A rename of the map (or of its type) would silently make
    every assertion above vacuous, so the shape is asserted rather than assumed."""
    labels = _tool_labels()
    assert "search" in labels, f"TOOL_LABELS parsed as {sorted(labels)} — the map moved?"
