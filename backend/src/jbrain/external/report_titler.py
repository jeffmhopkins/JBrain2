"""The `title_research_report` job: fill the NULL display title for one research report.

A deep-research report is keyed on the owner's raw `question` — often a whole paragraph —
so the Research Library needs a tight heading to show instead (migration 0141). This is a
single LLM one-shot (the `research.title` route, `low` tier) that distills the question
into a short title, the sibling of session auto-titling. Enqueued by `persist_report` as a
best-effort follow-up alongside the embedding job: a title failure retries on its own and
never blocks the report the owner already sees, and the listing falls back to the question
until the title lands.

Idempotent: re-checks `title IS NULL` on both read and write, so a concurrent re-run of the
same report at worst no-ops the row (mirroring `embed_research_report`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import scoped_session
from jbrain.llm import LlmRouter
from jbrain.llm.promptfile import load_prompt
from jbrain.queue import SYSTEM_CTX

log = structlog.get_logger()

_TASK = "research.title"
_TITLE = load_prompt(
    Path(__file__).parents[1] / "agent" / "prompts" / "research_report_title.prompt"
)
_TITLE_MAX_TOKENS = 400  # a headline plus the low-tier model's thinking trace
# The raw question can be a long paragraph; only its opening is needed to name it, and
# the excerpt grounds the title in what the report actually covered.
_QUESTION_CAP = 1200
_EXCERPT_CAP = 600
# A defensive cap: the prompt asks for ~60 chars, but a stray model can run long.
_TITLE_CAP = 90


def _clean_title(raw: str) -> str:
    """The model's reply reduced to one clean heading: first non-empty line, surrounding
    quotes and a leading "Title:" label stripped, whitespace collapsed, length-capped."""
    line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    if line[:6].lower() == "title:":
        line = line[6:].strip()
    line = line.strip("\"'“”").strip()
    line = " ".join(line.split())
    if len(line) > _TITLE_CAP:
        line = line[: _TITLE_CAP - 1].rstrip() + "…"
    return line


class ResearchReportTitler:
    """Generates the short display title for one research report via the LLM router."""

    def __init__(self, maker: async_sessionmaker[AsyncSession], router: LlmRouter):
        self._maker = maker
        self._router = router

    async def title_research_report(self, payload: dict[str, Any]) -> None:
        report_id = str(payload["report_id"])
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT question, summary FROM app.research_reports"
                        " WHERE id = :rid AND title IS NULL AND status = 'done'"
                    ),
                    {"rid": report_id},
                )
            ).first()
        if row is None:
            return  # already titled, or gone — a harmless no-op
        question = (row.question or "").strip()
        if not question:
            return
        user_text = f"Question:\n{question[:_QUESTION_CAP]}"
        if row.summary:
            user_text += f"\n\nReport excerpt:\n{row.summary[:_EXCERPT_CAP]}"
        result = await self._router.complete(
            _TASK,
            system=_TITLE.render(),
            user_text=user_text,
            max_tokens=_TITLE_MAX_TOKENS,
        )
        title = _clean_title(result.text or "")
        if not title:
            return  # a blank generation leaves the fallback in place; the retry may do better
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            await session.execute(
                text(
                    "UPDATE app.research_reports SET title = :title"
                    " WHERE id = :rid AND title IS NULL"
                ),
                {"rid": report_id, "title": title},
            )
        log.info("research_report.titled", report_id=report_id, title=title)
