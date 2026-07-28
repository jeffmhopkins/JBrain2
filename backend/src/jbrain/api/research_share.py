"""The PUBLIC research-report share surface — unauthenticated by design (the token is the
credential), the read side of the owner mint/list/revoke in `research_library.py`.

A visitor GETs `/research-share/{token}`: the token resolves to a live link, then the report
(or, for a folder link, the folder's current members) is read PINNED to the link id so the
`research_reports_share` RLS policy (0150) admits exactly that link's target and nothing else.
Revoked or unknown tokens 404. Every response is rate-limited, marked `no-referrer` (a clicked
citation must not leak the token via `Referer`) and `noindex` (crawlers must not cache token
URLs). This router carries NO owner dependency — do not add owner-only reads here.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic needs the runtime symbol
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from jbrain.api.research_service import ResearchLibrary
from jbrain.external.research_corpus import LibraryReport, ReportRecord
from jbrain.locations.ratelimit import TokenBucket

router = APIRouter()


def get_library(request: Request) -> ResearchLibrary:
    return cast(ResearchLibrary, request.app.state.research_library)


def get_share_limiter(request: Request) -> TokenBucket:
    return cast(TokenBucket, request.app.state.research_share_rate_limiter)


LibraryDep = Annotated[ResearchLibrary, Depends(get_library)]
ShareLimiterDep = Annotated[TokenBucket, Depends(get_share_limiter)]


class PublicReportOut(BaseModel):
    """A shared report's full body — the fields the report renderer needs, no owner metadata."""

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


class ShareReportItem(BaseModel):
    """One entry in a folder link's index."""

    id: str
    question: str
    title: str | None
    created_at: datetime | None


class ShareViewOut(BaseModel):
    """The resolved share: a single report, or a folder's label + its current members."""

    kind: str
    label: str | None
    report: PublicReportOut | None = None
    reports: list[ShareReportItem] | None = None


def _report_out(record: ReportRecord) -> PublicReportOut:
    return PublicReportOut(
        id=record.id,
        question=record.question,
        report_md=record.report_md,
        complexity=record.complexity,
        rounds=record.rounds,
        sub_agents=record.sub_agents,
        analyzed=record.analyzed,
        revised=record.revised,
        coverage_limited=record.coverage_limited,
        truncated=record.truncated,
        sources=record.sources,
        created_at=record.created_at,
    )


def _index_item(report: LibraryReport) -> ShareReportItem:
    return ShareReportItem(
        id=report.id, question=report.question, title=report.title, created_at=report.created_at
    )


def _harden(response: Response) -> None:
    # A shared report body embeds external citation URLs; a click must not send the token
    # in the Referer, and crawlers must not index the token URL.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"


def _rate_limit(request: Request, limiter: TokenBucket) -> None:
    client = request.client.host if request.client else "unknown"
    if not limiter.allow(client):
        raise HTTPException(status_code=429, detail="rate limited")


@router.get("/research-share/{token}")
async def view_share(
    token: str, request: Request, response: Response, lib: LibraryDep, limiter: ShareLimiterDep
) -> ShareViewOut:
    """Resolve a share link to its report (report link) or folder index (folder link). 404 on
    an unknown / revoked token."""
    _rate_limit(request, limiter)
    _harden(response)
    resolved = await lib.resolve_share(token)
    if resolved is None:
        raise HTTPException(status_code=404, detail="unknown or revoked share link")
    if resolved.target_kind == "report":
        record = await lib.fetch_shared_report(resolved.id, resolved.report_id or "")
        if record is None:  # target vanished (a hard report delete cascades the link away)
            raise HTTPException(status_code=404, detail="unknown or revoked share link")
        return ShareViewOut(kind="report", label=resolved.label, report=_report_out(record))
    reports = await lib.list_group_reports(resolved.id)
    return ShareViewOut(
        kind="group", label=resolved.label, reports=[_index_item(r) for r in reports]
    )


@router.get("/research-share/{token}/reports/{report_id}")
async def view_share_report(
    token: str,
    report_id: str,
    request: Request,
    response: Response,
    lib: LibraryDep,
    limiter: ShareLimiterDep,
) -> PublicReportOut:
    """One report within a share (a folder member, or the report link's own target). RLS makes
    a report the link doesn't grant a natural 404. 404 on an unknown / revoked token too."""
    _rate_limit(request, limiter)
    _harden(response)
    resolved = await lib.resolve_share(token)
    if resolved is None:
        raise HTTPException(status_code=404, detail="unknown or revoked share link")
    record = await lib.fetch_shared_report(resolved.id, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such report in this share")
    return _report_out(record)
