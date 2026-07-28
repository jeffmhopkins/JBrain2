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

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

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
    # The short display heading (title_research_report job); None until it lands.
    title: str | None
    complexity: str
    created_at: datetime | None
    sub_agents: int
    rounds: int
    # The owner's folder this report is filed under (None = the trailing "Ungrouped"
    # section); owner-only browse metadata the Reports tab groups by.
    group_id: str | None = None
    # `web` | `library` | `library_first` — lets the Share sheet warn before publishing a
    # report drawn from the owner's private notes.
    source_mode: str = "web"


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


# --- report-folder models --------------------------------------------------------------


class ReportGroupOut(BaseModel):
    id: str
    name: str
    position: int


class ReportGroupBody(BaseModel):
    """Create / rename payload for a report folder."""

    name: str = Field(min_length=1, max_length=80)


class MoveReportBody(BaseModel):
    """File a report into a folder (None = the trailing "Ungrouped" section)."""

    group_id: str | None = None


# --- share-link models -----------------------------------------------------------------
# A public, revocable, no-login read grant for one report or one folder (migration 0150).

# source modes that synthesize the owner's private notes into the report body — sharing one
# publicly leaks note content RLS can't catch, so the owner is warned at mint / in the list.
_LIBRARY_MODES = frozenset({"library", "library_first"})


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class MintShareBody(BaseModel):
    """Mint a share link for a single report or a whole folder (exactly one target)."""

    target_kind: Literal["report", "group"]
    report_id: str | None = None
    group_id: str | None = None
    # Optional owner note; for a folder link the folder name is snapshotted here at mint.
    label: str | None = Field(default=None, max_length=200)


class MintShareOut(BaseModel):
    id: str
    target_kind: str
    label: str | None
    # The bearer secret — shown EXACTLY once; the client builds `${origin}/share/${token}`.
    token: str
    # The target is a report synthesized from the owner's private notes (warn before sharing).
    library_warning: bool


class ShareOut(BaseModel):
    id: str
    label: str | None
    target_kind: str
    report_id: str | None
    group_id: str | None
    created_at: datetime | None
    last_viewed_at: datetime | None
    view_count: int
    library_warning: bool


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


# --- report folders --------------------------------------------------------------------
# Owner-only browse metadata (migration 0149), mirroring the Tasks surface's task-groups.
# Every folder op runs under the FULL-owner context; the move validates the report is in
# scope and the destination folder is the owner's before it writes the opaque group_id.


@router.get("/report-groups")
async def list_report_groups(request: Request, principal: OwnerDep) -> list[ReportGroupOut]:
    groups = await get_library(request).list_report_groups(ctx_for(principal))
    return [ReportGroupOut(**vars(g)) for g in groups]


@router.post("/report-groups", status_code=201)
async def create_report_group(
    request: Request, principal: OwnerDep, body: ReportGroupBody
) -> ReportGroupOut:
    created = await get_library(request).create_report_group(
        ctx_for(principal), name=body.name.strip()
    )
    return ReportGroupOut(**vars(created))


@router.patch("/report-groups/{group_id}")
async def rename_report_group(
    request: Request, principal: OwnerDep, group_id: str, body: ReportGroupBody
) -> ReportGroupOut:
    updated = await get_library(request).rename_report_group(
        ctx_for(principal), group_id, name=body.name.strip()
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such folder")
    return ReportGroupOut(**vars(updated))


@router.delete("/report-groups/{group_id}", status_code=204)
async def delete_report_group(request: Request, principal: OwnerDep, group_id: str) -> None:
    # Its reports fall to Ungrouped (FK SET NULL); they are never deleted.
    await get_library(request).delete_report_group(ctx_for(principal), group_id)


@router.patch("/reports/{report_id}/group", status_code=204)
async def move_report(
    request: Request, principal: OwnerDep, report_id: str, body: MoveReportBody
) -> None:
    # Resolve the report under the owner's read scope first (tolerates a non-uuid id and
    # confirms it exists) — a missing report is a clean 404, not a silent no-op. A non-null
    # destination must be a folder the owner owns; the write runs under full-owner context.
    lib = get_library(request)
    ctx = ctx_for(principal)
    record = await lib.fetch_report(principal.id, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no report with that id in scope")
    if body.group_id is not None and not await lib.report_group_exists(ctx, body.group_id):
        raise HTTPException(status_code=404, detail="no such folder")
    await lib.set_report_group(ctx, record.id, body.group_id)


# --- share links -----------------------------------------------------------------------
# Owner mint / list / revoke for public report share links (migration 0150). The public
# read side is a SEPARATE, unauthenticated router (api/research_share.py). Mint validates the
# target is in the owner's scope before persisting; a folder link snapshots the folder name.


@router.post("/shares", status_code=201)
async def mint_share(request: Request, principal: OwnerDep, body: MintShareBody) -> MintShareOut:
    """Mint a public share link for one report or one folder (owner only). Returns the secret
    once. 404 when the target isn't in the owner's scope; 422 on a target/kind mismatch."""
    lib = get_library(request)
    library_warning = False
    label = body.label
    if body.target_kind == "report":
        # A report link is by id only — reject a question-string ref (fetch_report also accepts
        # one) so a share can't resolve a different report than the caller named.
        if not body.report_id or body.group_id is not None or not _is_uuid(body.report_id):
            raise HTTPException(status_code=422, detail="a report link needs a report_id (uuid)")
        record = await lib.fetch_report(principal.id, body.report_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no report with that id in scope")
        report_id, group_id = record.id, None
        library_warning = record.source_mode in _LIBRARY_MODES
    else:
        if not body.group_id or body.report_id is not None:
            raise HTTPException(status_code=422, detail="a folder link needs group_id only")
        ctx = ctx_for(principal)
        folder = next(
            (g for g in await lib.list_report_groups(ctx) if g.id == body.group_id), None
        )
        if folder is None:
            raise HTTPException(status_code=404, detail="no such folder")
        report_id, group_id = None, folder.id
        label = label or folder.name  # snapshot the folder name for the public index
        # Warn if the folder currently holds a notes-derived report (the report path warns from
        # the target's own source_mode; a folder must check its members).
        library_warning = await lib.group_has_library_report(ctx, folder.id)
    token, link = await lib.mint_share(
        ctx_for(principal),
        target_kind=body.target_kind,
        report_id=report_id,
        group_id=group_id,
        label=label,
    )
    return MintShareOut(
        id=link.id,
        target_kind=link.target_kind,
        label=link.label,
        token=token,
        library_warning=library_warning,
    )


@router.get("/shares")
async def list_shares(request: Request, principal: OwnerDep) -> list[ShareOut]:
    """The owner's live share links — metadata + usage, never the secret."""
    links = await get_library(request).list_shares(ctx_for(principal))
    return [
        ShareOut(
            id=s.id,
            label=s.label,
            target_kind=s.target_kind,
            report_id=s.report_id,
            group_id=s.group_id,
            created_at=s.created_at,
            last_viewed_at=s.last_viewed_at,
            view_count=s.view_count,
            library_warning=s.library_warning,
        )
        for s in links
    ]


@router.delete("/shares/{share_id}", status_code=204)
async def revoke_share(request: Request, principal: OwnerDep, share_id: str) -> None:
    """Revoke a share link (owner only). 404 on an unknown / already-revoked id. The public
    read fails closed on the next visit (the RLS policy re-checks revocation)."""
    if not await get_library(request).revoke_share(ctx_for(principal), share_id):
        raise HTTPException(status_code=404, detail="no such share link")


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
    lib = get_library(request)
    record = await lib.fetch_video(principal.id, video_id)
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
        # Redeem each frame's stored thumb_id into an inline thumbnail so the detail's
        # filmstrip renders the same stills the in-chat analyze card shows.
        frames=await lib.resolve_frames(list(record.frames)),
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
