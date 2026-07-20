"""Persist a completed deep-research report into the report library, and search/read it back.

The single writer + reader for `app.research_reports` (0140), mirroring the external-video
corpus (`external.corpus`): the `deep_research` tool calls `persist_report` when a run finishes,
and jerv's report tools (list / search / read / show / remove_research_report) read it back under
the corpus `external` scope (migration 0136), so a persona whose own session is empty-scoped (jerv)
reaches the report library and NOTHING owner-authored. A report has no timeline, so — unlike the
video corpus — there are no passage chunks: search is a single report-level hybrid (FTS over the
generated `tsv` + a `summary_embedding` dense leg, fused RRF).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.embed import EmbedClient, vector_literal
from jbrain.queue import SYSTEM_CTX, enqueue
from jbrain.search.service import rrf_scores
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

KIND_EMBED_RESEARCH_REPORT = "embed_research_report"
KIND_TITLE_RESEARCH_REPORT = "title_research_report"

# In-code only (dispatch-only, no Ops trigger, like embed_external_source): the report sibling of
# embed_note, kicked as a follow-up by persist_report. The worker adds it to its build_registry
# tuple; the boot-time registry bijection requires a spec per handler.
EMBED_RESEARCH_REPORT_SPEC = ActionSpec(
    name=KIND_EMBED_RESEARCH_REPORT,
    version=1,
    handler=KIND_EMBED_RESEARCH_REPORT,
    domain_optional=True,
    mutating=True,  # writes the summary embedding
    cost_class="standard",  # local embed container, no LLM router
    description="Fill the NULL summary embedding for one research report.",
    category="maintenance",
)

# The report-title sibling: an LLM one-shot (external.report_titler) that distills the
# raw question into a short display title, also kicked as a persist_report follow-up.
# An LLM call (cost_class "expensive"), unlike the local-container embed job.
TITLE_RESEARCH_REPORT_SPEC = ActionSpec(
    name=KIND_TITLE_RESEARCH_REPORT,
    version=1,
    handler=KIND_TITLE_RESEARCH_REPORT,
    domain_optional=True,
    mutating=True,  # writes the display title
    cost_class="expensive",  # a small LLM completion via the router
    description="Generate the short display title for one research report.",
    category="maintenance",
)

_SUMMARY_LEN = 600  # a report's opening, for the listing display + the summary embedding
_LEG_LIMIT = 40  # per-leg candidate cap before RRF fusion (~ SearchService)


def _question_hash(question: str) -> str:
    """A stable dedup key: sha256 of the whitespace-collapsed, lowercased question, so a re-run
    of the same question upserts in place (the newest report wins)."""
    normalized = " ".join(question.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _summary_excerpt(report_md: str) -> str:
    """A short plain-text opening of the report for the listing + the summary vector — the body
    with Markdown scaffolding (heading #'s, bold/italic/code marks, quote >) stripped so it never
    pollutes FTS or the embedding, whitespace-collapsed, capped."""
    plain = re.sub(r"[#*`>]", "", report_md).strip()
    return " ".join(plain.split())[:_SUMMARY_LEN]


def _report_read_context(principal_id: str) -> SessionContext:
    """The corpus `external` scope (migration 0136), identical to the video-corpus tools: an owner
    session restricted to the corpus's own `external` domain ONLY, so jerv reaches the report
    library and nothing owner-authored, and the same context serves the removal-proposal staging."""
    return SessionContext(
        principal_id=principal_id,
        principal_kind="owner",
        domain_scopes=("external",),
        owner_scoped=True,
    )


async def persist_report(
    maker: async_sessionmaker[AsyncSession],
    *,
    session_id: str | None,
    question: str,
    report_md: str,
    complexity: str,
    rounds: int,
    sub_agents: int,
    analyzed: bool,
    revised: bool,
    coverage_limited: bool,
    truncated: bool,
    sources: list[dict[str, Any]],
    tool: str | None = None,
) -> str:
    """Upsert the library row for one completed report and enqueue its summary embedding. Keyed on
    the question hash: a re-run of the same question replaces the older report (newest wins) and
    NULLs the embedding so `embed_research_report` re-fills it. Runs under `SYSTEM_CTX`."""
    params = {
        "session_id": session_id or None,
        "question": question,
        "question_hash": _question_hash(question),
        "report_md": report_md,
        "summary": _summary_excerpt(report_md) or None,
        "complexity": complexity or None,
        "rounds": rounds,
        "sub_agents": sub_agents,
        "analyzed": analyzed,
        "revised": revised,
        "coverage_limited": coverage_limited,
        "truncated": truncated,
        "sources": json.dumps(sources or []),
        "tool": tool,
    }
    async with scoped_session(maker, SYSTEM_CTX) as session:
        report_id = (
            await session.execute(
                text(
                    "INSERT INTO app.research_reports"
                    " (session_id, question, question_hash, report_md, summary, complexity,"
                    "  rounds, sub_agents, analyzed, revised, coverage_limited, truncated,"
                    "  sources, tool, status)"
                    " VALUES (cast(:session_id AS uuid), :question, :question_hash, :report_md,"
                    "  :summary, :complexity, :rounds, :sub_agents, :analyzed, :revised,"
                    "  :coverage_limited, :truncated, cast(:sources AS jsonb), :tool, 'done')"
                    " ON CONFLICT (question_hash) DO UPDATE SET"
                    "  session_id = EXCLUDED.session_id, question = EXCLUDED.question,"
                    "  report_md = EXCLUDED.report_md, summary = EXCLUDED.summary,"
                    "  complexity = EXCLUDED.complexity, rounds = EXCLUDED.rounds,"
                    "  sub_agents = EXCLUDED.sub_agents, analyzed = EXCLUDED.analyzed,"
                    "  revised = EXCLUDED.revised, coverage_limited = EXCLUDED.coverage_limited,"
                    "  truncated = EXCLUDED.truncated, sources = EXCLUDED.sources,"
                    "  tool = EXCLUDED.tool, status = 'done', created_at = now(),"
                    # NULL the derived slots on a re-run so the follow-up jobs re-fill
                    # them against the newest report (title tracks the question, which
                    # is stable, but the report body changed).
                    "  summary_embedding = NULL, embedding_model = NULL, title = NULL"
                    " RETURNING id"
                ),
                params,
            )
        ).scalar_one()
    report_id = str(report_id)
    await enqueue(maker, SYSTEM_CTX, KIND_EMBED_RESEARCH_REPORT, {"report_id": report_id})
    await enqueue(maker, SYSTEM_CTX, KIND_TITLE_RESEARCH_REPORT, {"report_id": report_id})
    log.info("research_report.persisted", report_id=report_id, question=question[:80])
    return report_id


@dataclass(frozen=True)
class LibraryReport:
    """One report in a library listing — the metadata a browse/count needs, no body."""

    id: str
    question: str
    # The short display heading (title_research_report job); None until it lands, so the
    # client falls back to the question.
    title: str | None
    complexity: str
    created_at: datetime | None
    sub_agents: int
    rounds: int


async def list_reports(
    maker: async_sessionmaker[AsyncSession],
    *,
    limit: int,
    offset: int = 0,
    principal_id: str = "",
) -> tuple[list[LibraryReport], int]:
    """A page of the report library (newest first) plus the library's TOTAL size — the engine
    behind `list_research_reports`, so the owner can enumerate or count what's been researched
    without a fuzzy search. Reads under the corpus `external` scope."""
    async with scoped_session(maker, _report_read_context(principal_id)) as session:
        total = (
            await session.execute(
                text("SELECT count(*) FROM app.research_reports WHERE status = 'done'")
            )
        ).scalar_one()
        if total == 0 or offset >= total:
            return [], int(total)
        rows = (
            await session.execute(
                text(
                    "SELECT id, question, title, complexity, created_at, sub_agents, rounds"
                    " FROM app.research_reports WHERE status = 'done'"
                    " ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset"
                ),
                {"limit": limit, "offset": offset},
            )
        ).all()
    return [
        LibraryReport(
            id=str(r.id),
            question=r.question or "",
            title=(r.title or None),
            complexity=r.complexity or "",
            created_at=r.created_at,
            sub_agents=int(r.sub_agents or 0),
            rounds=int(r.rounds or 1),
        )
        for r in rows
    ], int(total)


_FTS_SQL = (
    "SELECT id, question, summary FROM app.research_reports"
    " WHERE status = 'done' AND tsv @@ websearch_to_tsquery('english', :q)"
    " ORDER BY ts_rank(tsv, websearch_to_tsquery('english', :q)) DESC, id LIMIT :limit"
)
_DENSE_SQL = (
    "SELECT id, question, summary FROM app.research_reports"
    " WHERE status = 'done' AND summary_embedding IS NOT NULL"
    " ORDER BY summary_embedding <=> cast(:qvec AS vector), id LIMIT :limit"
)


@dataclass(frozen=True)
class ReportHit:
    """One report in a search result: its question + a short excerpt for the citation line."""

    id: str
    question: str
    excerpt: str


async def search_reports(
    maker: async_sessionmaker[AsyncSession],
    embedder: EmbedClient,
    query: str,
    limit: int,
    *,
    principal_id: str = "",
) -> tuple[list[ReportHit], bool]:
    """Hybrid RRF search over the report library (FTS + summary-embedding legs, fused). Returns
    (hits, degraded); `degraded` is True when the embed container was unreachable and only the
    keyword leg ran (mirrors SearchService / the video corpus)."""
    try:
        qvec: list[float] | None = (await embedder.embed([query]))[0]
    except Exception:  # noqa: BLE001 - embed container down → keyword-only, like SearchService
        qvec, degraded = None, True
    else:
        degraded = False

    display: dict[str, ReportHit] = {}
    rankings: list[list[str]] = []

    def ingest_rows(rows: Sequence[Any]) -> list[str]:
        ranking: list[str] = []
        for r in rows:
            rid = str(r.id)
            if rid not in display:
                display[rid] = ReportHit(
                    id=rid, question=r.question or "", excerpt=(r.summary or "").strip()
                )
            if rid not in ranking:
                ranking.append(rid)
        return ranking

    async with scoped_session(maker, _report_read_context(principal_id)) as session:
        fts = (await session.execute(text(_FTS_SQL), {"q": query, "limit": _LEG_LIMIT})).all()
        rankings.append(ingest_rows(fts))
        if qvec is not None:
            dense = (
                await session.execute(
                    text(_DENSE_SQL), {"qvec": vector_literal(qvec), "limit": _LEG_LIMIT}
                )
            ).all()
            rankings.append(ingest_rows(dense))

    scores = rrf_scores(*rankings)
    ranked = sorted(scores, key=lambda s: (scores[s], s), reverse=True)[:limit]
    return [display[rid] for rid in ranked], degraded


@dataclass(frozen=True)
class ReportRecord:
    """One report read in full: the report Markdown plus every slot the `deep_research_report`
    view rebuild (show_research_report) needs, so a stored report re-renders exactly as it did
    when first produced."""

    id: str
    question: str
    report_md: str
    complexity: str
    rounds: int
    sub_agents: int
    analyzed: bool
    revised: bool
    coverage_limited: bool
    truncated: bool
    sources: list[dict[str, Any]]
    created_at: datetime | None


_SELECT_RECORD = (
    "SELECT id, question, report_md, complexity, rounds, sub_agents, analyzed, revised,"
    " coverage_limited, truncated, sources, created_at FROM app.research_reports"
)


def _row_to_record(row: Any) -> ReportRecord:
    return ReportRecord(
        id=str(row.id),
        question=row.question or "",
        report_md=row.report_md or "",
        complexity=row.complexity or "",
        rounds=int(row.rounds or 1),
        sub_agents=int(row.sub_agents or 0),
        analyzed=bool(row.analyzed),
        revised=bool(row.revised),
        coverage_limited=bool(row.coverage_limited),
        truncated=bool(row.truncated),
        sources=list(row.sources or []),
        created_at=row.created_at,
    )


async def fetch_report(
    maker: async_sessionmaker[AsyncSession],
    ref: str,
    *,
    principal_id: str = "",
) -> ReportRecord | None:
    """One stored report in full, by its library id (a uuid) OR by an exact question (hashed) —
    so a follow-up turn can say `read_research_report(id=…)` from a listing, or pass the question
    text directly. None when nothing matches. Reads under the corpus `external` scope."""
    ref = (ref or "").strip()
    if not ref:
        return None
    try:
        uuid.UUID(ref)
        by_id = True
    except ValueError:
        by_id = False
    async with scoped_session(maker, _report_read_context(principal_id)) as session:
        if by_id:
            row = (
                await session.execute(
                    text(f"{_SELECT_RECORD} WHERE id = cast(:r AS uuid) AND status = 'done'"),
                    {"r": ref},
                )
            ).first()
        else:
            row = (
                await session.execute(
                    text(f"{_SELECT_RECORD} WHERE question_hash = :h AND status = 'done'"),
                    {"h": _question_hash(ref)},
                )
            ).first()
    return _row_to_record(row) if row is not None else None


async def delete_report(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, report_id: str
) -> bool:
    """Hard-delete one library report. Runs at proposal enact under the OWNER's context — the
    trusted executor, never jerv — after the owner approved the removal. Returns True when a row
    was actually removed (idempotent: a re-enact or an already-gone report is a no-op)."""
    async with scoped_session(maker, ctx) as session:
        deleted = (
            await session.execute(
                text("DELETE FROM app.research_reports WHERE id = cast(:id AS uuid) RETURNING id"),
                {"id": report_id},
            )
        ).first()
    return deleted is not None
