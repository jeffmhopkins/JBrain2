"""The Talk-board Editor turn (Phase 6, Wave T2 — `docs/mocks/wiki-talk-b-topics.html`).

Drives one agent turn for an owner's reply in a discussion topic: the Editor reads the article's
sourcing and replies in the thread, enacting via the sanctioned wiki tools (file_correction /
add_source_exclusion / request_rebuild) when the owner is right. Reuses `AgentLoop.run()` with a
dedicated Editor system prompt (the Full Brain prompt would tell it to stage, not enact) and the
owner's full-read context — it's an owner-only surface, so the cross-domain firewall (which guards
P7 scoped tokens) doesn't constrain it, and the write tools' WITH CHECK passes for any of the
article's domains.

The outcome chip is derived from a `_ToolTally` recorder (the loop records `kind='tool'` per call);
the reply is posted whenever the turn produced prose OR a write lever fired (so an enacted action is
never invisible, even if a wall-clock timeout cancels the turn after the lever committed).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from jbrain.agent.loop import AgentLoop
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.db.session import SessionContext
from jbrain.llm.promptfile import load_prompt
from jbrain.llm.router import LlmRouter
from jbrain.llm.types import AssistantMessage, LlmMessage, UserMessage

log = structlog.get_logger()

# Full owner read: every wiki tool is offered and the owner sees all domains (owner-only endpoint).
ALL_DOMAINS = ("general", "health", "finance", "location")
# Wall-clock bound on the turn; on timeout a chip-only reply is still posted if a lever committed.
EDITOR_TIMEOUT_S = 60.0

WIKI_EDITOR_PROMPT = load_prompt(
    Path(__file__).parent.parent / "agent" / "prompts" / "wiki_editor.prompt"
).render()

# Successful write tool → (outcome chip, fallback body), in precedence order: a correction is the
# headline action, then an exclusion, then a bare rebuild. The tools only *queue* the rebuild.
_CHIPS: tuple[tuple[str, str, str], ...] = (
    ("file_correction", "correction filed → rebuild queued", "Filed your correction."),
    ("add_source_exclusion", "source excluded · rebuild queued", "Excluded that source."),
    ("request_rebuild", "rebuild queued", "Queued a rebuild."),
)


@dataclass(frozen=True)
class EditorReply:
    body: str
    outcome: str | None


class _ToolTally:
    """A `RunRecorder`: records the names of SUCCESSFUL tool steps so the chip reflects the lever
    the Editor actually pulled (a rejected tool is recorded with ok=False and ignored)."""

    def __init__(self) -> None:
        self.tools: list[str] = []

    async def step(self, *, idx: int, kind: str, name: str, ok: bool, cost_tokens: int) -> None:
        if kind == "tool" and ok:
            self.tools.append(name)


def _outcome(tally: _ToolTally) -> tuple[str | None, str | None]:
    """(chip, fallback_body) for the highest-precedence successful write tool, else (None, None)."""
    for name, chip, fallback in _CHIPS:
        if name in tally.tools:
            return chip, fallback
    return None, None


def _conversation(posts: list[dict[str, Any]]) -> list[LlmMessage]:
    """Map the topic's posts to the turn's conversation (builder posts skipped — not dialogue).
    The article/topic context rides the system prompt, so this is purely the owner↔Editor turns."""
    out: list[LlmMessage] = []
    for post in posts:
        if post["author"] == "owner":
            out.append(UserMessage(text=post["body"]))
        elif post["author"] == "editor":
            out.append(AssistantMessage(text=post["body"]))
    return out


async def run_editor_turn(
    router: LlmRouter,
    registry: ToolRegistry,
    ctx: SessionContext,
    *,
    article_id: str,
    article_title: str,
    topic_title: str,
    posts: list[dict[str, Any]],
    timezone: str | None = None,
) -> EditorReply | None:
    """Run one Editor turn over the topic so far. Returns the reply to post (prose, or a chip-only
    line when a lever fired without prose), or None when nothing happened."""
    conversation = _conversation(posts)
    if not conversation:
        return None
    system = (
        f"{WIKI_EDITOR_PROMPT}\n\nArticle: {article_title} (id {article_id}).\n"
        f"Talk topic: {topic_title}."
    )
    tally = _ToolTally()
    loop = AgentLoop(router, registry, recorder=tally)
    text = ""
    try:
        result = await asyncio.wait_for(
            loop.run(
                session=ctx,
                scopes=ALL_DOMAINS,
                conversation=conversation,
                timezone=timezone,
                system=system,
            ),
            timeout=EDITOR_TIMEOUT_S,
        )
        text = result.text.strip()
    except Exception:  # noqa: BLE001 — best-effort turn; a committed lever still reports below
        log.warning("wiki_editor_turn_failed", article_id=article_id)
    chip, fallback = _outcome(tally)
    if text:
        return EditorReply(body=text, outcome=chip)
    if chip is not None:
        return EditorReply(body=fallback or "Done.", outcome=chip)
    return None
