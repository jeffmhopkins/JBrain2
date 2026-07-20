"""The Research Library API — the owner-only browse surface behind the card-launcher
"Research" tile (build plan: docs/plans/RESEARCH_LIBRARY_PLAN.md).

The human's door to the two `external`-corpus artifacts jerv produces on its own turns:
deep-research reports (`app.research_reports`) and analysed videos (`app.external_sources`).
Both already persist server-side and are reachable to *jerv* via corpus tools; this router
lets the OWNER list, search, view, and delete them without a jerv turn.

Reads pass `principal.id` to the corpus readers, which build the purpose-built `external`
scope internally (the same firewall the jerv tools use). Deletes are owner-initiated and
direct — a full-owner `ctx_for(principal)` is the trusted executor (never jerv), so they
call the corpus delete callables straight, not the proposal/executor path. The whole router
is owner-gated (`owner_only`); a capability/device token 403s before any read.
"""

from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from jbrain.api.deps import owner_only
from jbrain.api.notes import ctx_for
from jbrain.api.research_service import ResearchLibrary
from jbrain.auth.service import PrincipalInfo

router = APIRouter(prefix="/research-library", dependencies=[Depends(owner_only)])

OwnerDep = Annotated[PrincipalInfo, Depends(owner_only)]

# A generous-but-bounded page; the library is browsed, not bulk-scanned. The client may ask
# for fewer, or up to MAX_LIMIT, but never an unbounded read.
PAGE_LIMIT = 50
MAX_LIMIT = 200
SEARCH_LIMIT = 30
MAX_SEARCH_LIMIT = 60


def get_library(request: Request) -> ResearchLibrary:
    return cast(ResearchLibrary, request.app.state.research_library)


# --- report models ---------------------------------------------------------------------


class ReportListOut(BaseModel):
    id: str
    question: str
    complexity: str
    created_at: datetime | None
    sub_agents: int
    rounds: int


class ReportHitOut(BaseModel):
    id: str
    question: str
    excerpt: str


class ReportDetailOut(BaseModel):
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


class ReportListResponse(BaseModel):
    items: list[ReportListOut]
    total: int


class ReportSearchResponse(BaseModel):
    items: list[ReportHitOut]
    degraded: bool


# --- video models ----------------------------------------------------------------------


class VideoListOut(BaseModel):
    video_id: str
    provider: str
    title: str
    channel_name: str
    url: str
    published_at: datetime | None
    duration_s: int | None


class VideoHitOut(BaseModel):
    video_id: str
    source_id: str
    title: str
    channel_name: str
    url: str
    passage: str
    t_ms: int | None


class TranscriptWindowOut(BaseModel):
    t_ms: int
    text: str


class VideoDetailOut(BaseModel):
    source_id: str
    video_id: str
    provider: str
    title: str
    channel_name: str
    url: str
    transcript_source: str
    summary: str
    duration_s: int | None
    duration_ms: int | None
    published_at: datetime | None
    windows: list[TranscriptWindowOut]
    frames: list[dict[str, Any]]
    cued_transcript: dict[str, Any] | None


class VideoListResponse(BaseModel):
    items: list[VideoListOut]
    total: int


class VideoSearchResponse(BaseModel):
    items: list[VideoHitOut]
    degraded: bool


# --- reports ---------------------------------------------------------------------------


@router.get("/reports")
async def list_reports(
    request: Request,
    principal: OwnerDep,
    limit: int = PAGE_LIMIT,
    offset: int = 0,
) -> ReportListResponse:
    reports, total = await get_library(request).list_reports(
        principal.id, limit=max(1, min(limit, MAX_LIMIT)), offset=max(0, offset)
    )
    return ReportListResponse(items=[ReportListOut(**vars(r)) for r in reports], total=total)


# Declared before "/reports/{report_id}" so the literal path wins the route match.
@router.get("/reports/search")
async def search_reports(
    request: Request,
    principal: OwnerDep,
    q: Annotated[str, Query(min_length=1)],
    limit: int = SEARCH_LIMIT,
) -> ReportSearchResponse:
    hits, degraded = await get_library(request).search_reports(
        principal.id, q, max(1, min(limit, MAX_SEARCH_LIMIT))
    )
    return ReportSearchResponse(items=[ReportHitOut(**vars(h)) for h in hits], degraded=degraded)


@router.get("/reports/{report_id}")
async def get_report(request: Request, principal: OwnerDep, report_id: str) -> ReportDetailOut:
    record = await get_library(request).fetch_report(principal.id, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no report with that id in scope")
    return ReportDetailOut(**vars(record))


@router.delete("/reports/{report_id}", status_code=204)
async def delete_report(request: Request, principal: OwnerDep, report_id: str) -> None:
    # Resolve within the owner's external read scope first — this tolerates a non-uuid id
    # (a clean 204 no-op instead of a 500 from `cast(:id AS uuid)`) and confirms the row is
    # in-scope before the full-owner hard delete. Owner-initiated, so it bypasses jerv's
    # proposal/approval path; idempotent (an already-gone report is a harmless no-op).
    lib = get_library(request)
    record = await lib.fetch_report(principal.id, report_id)
    if record is not None:
        await lib.delete_report(ctx_for(principal), record.id)


# --- videos ----------------------------------------------------------------------------


@router.get("/videos")
async def list_videos(
    request: Request,
    principal: OwnerDep,
    limit: int = PAGE_LIMIT,
    offset: int = 0,
) -> VideoListResponse:
    videos, total = await get_library(request).list_videos(
        principal.id, limit=max(1, min(limit, MAX_LIMIT)), offset=max(0, offset)
    )
    return VideoListResponse(items=[VideoListOut(**vars(v)) for v in videos], total=total)


@router.get("/videos/search")
async def search_videos(
    request: Request,
    principal: OwnerDep,
    q: Annotated[str, Query(min_length=1)],
    limit: int = SEARCH_LIMIT,
) -> VideoSearchResponse:
    hits, degraded = await get_library(request).search_videos(
        principal.id, q, max(1, min(limit, MAX_SEARCH_LIMIT))
    )
    return VideoSearchResponse(items=[VideoHitOut(**vars(h)) for h in hits], degraded=degraded)


@router.get("/videos/{video_id}")
async def get_video(request: Request, principal: OwnerDep, video_id: str) -> VideoDetailOut:
    record = await get_library(request).fetch_video(principal.id, video_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no analysed video with that id in scope")
    return VideoDetailOut(
        source_id=record.source_id,
        video_id=record.video_id,
        provider=record.provider,
        title=record.title,
        channel_name=record.channel_name,
        url=record.url,
        transcript_source=record.transcript_source,
        summary=record.summary,
        duration_s=record.duration_s,
        duration_ms=record.duration_ms,
        published_at=record.published_at,
        windows=[TranscriptWindowOut(t_ms=t, text=txt) for t, txt in record.windows],
        frames=list(record.frames),
        cued_transcript=record.cued_transcript,
    )


@router.delete("/videos/{video_id}", status_code=204)
async def delete_video(request: Request, principal: OwnerDep, video_id: str) -> None:
    # Resolve the row id under the owner's read scope, then hard-delete it under the full-owner
    # context. Missing/already-gone → a harmless no-op 204 (idempotent).
    lib = get_library(request)
    record = await lib.fetch_video(principal.id, video_id)
    if record is not None:
        await lib.delete_video(ctx_for(principal), record.source_id)
