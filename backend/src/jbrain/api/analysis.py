"""Analysis read/resolve endpoints: per-note extraction view, entity pages,
and the review inbox. Owner-only is implicit pre-P7: every query runs on the
principal's RLS context, and only the owner holds a session today.

The response shapes are a frozen contract with the frontend — change them
only with a coordinated frontend PR.
"""

from typing import Annotated, Any, cast

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from jbrain.analysis.repo import (
    REVIEW_STATUSES,
    AlreadyOpen,
    AlreadyResolved,
    SqlAnalysisRepo,
    UnknownAction,
)
from jbrain.api.deps import OwnerDep, PrincipalDep
from jbrain.api.images import MAX_IMAGE_BYTES, sniff_image_type, sniff_path
from jbrain.api.notes import BlobStoreDep, ctx_for
from jbrain.embed import EmbedClient

router = APIRouter()


def get_analysis_repo(request: Request) -> SqlAnalysisRepo:
    return cast(SqlAnalysisRepo, request.app.state.analysis_repo)


def get_embed_client(request: Request) -> EmbedClient:
    return cast(EmbedClient, request.app.state.embed_client)


@router.get("/notes/{note_id}/analysis")
async def note_analysis(note_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    view = await get_analysis_repo(request).note_analysis_view(ctx_for(principal), note_id)
    if view is None:
        raise HTTPException(status_code=404, detail="note not found")
    return view


@router.get("/entities")
async def entity_list(
    request: Request,
    principal: PrincipalDep,
    q: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    items = await get_analysis_repo(request).list_entities(ctx_for(principal), q=q, kind=kind)
    return {"items": items}


@router.get("/entities/{entity_id}")
async def entity_detail(
    entity_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    view = await get_analysis_repo(request).entity_view(ctx_for(principal), entity_id)
    if view is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return view


@router.get("/entities/{entity_id}/neighbors")
async def entity_neighbors(
    entity_id: str,
    request: Request,
    principal: PrincipalDep,
    depth: Annotated[int, Query(ge=1, le=2)] = 1,
) -> dict[str, Any]:
    """Ego subgraph for the graph view (nodes + directed edges to `depth`
    hops). RLS-scoped, so firewalled neighbours and their edges never leak."""
    view = await get_analysis_repo(request).ego_graph(ctx_for(principal), entity_id, depth=depth)
    if view is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return view


@router.put("/entities/{entity_id}/image")
async def set_entity_image(
    entity_id: str, file: UploadFile, owner: OwnerDep, request: Request, blobs: BlobStoreDep
) -> dict[str, str]:
    """Set an entity's owner profile image. Owner-only; the media type is sniffed from the bytes
    (the client's Content-Type is not trusted), so a non-image is rejected before it is stored."""
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image too large")
    media_type = sniff_image_type(data[:16])
    if media_type is None:
        raise HTTPException(status_code=415, detail="unsupported image type")
    digest = await blobs.put(data)
    if not await get_analysis_repo(request).set_entity_image(ctx_for(owner), entity_id, digest):
        raise HTTPException(status_code=404, detail="entity not found")
    return {"image_sha": digest, "media_type": media_type}


@router.get("/entities/{entity_id}/image")
async def get_entity_image(
    entity_id: str, principal: PrincipalDep, request: Request, blobs: BlobStoreDep
) -> FileResponse:
    sha = await get_analysis_repo(request).entity_image_sha(ctx_for(principal), entity_id)
    if sha is None or not await blobs.exists(sha):
        raise HTTPException(status_code=404, detail="no image")
    path = blobs.path_for(sha)
    # nosniff: the bytes are served inline with a magic-byte-derived type, so a browser must not
    # re-sniff a chameleon file (image header + HTML tail) into something executable.
    return FileResponse(
        path, media_type=sniff_path(path), headers={"X-Content-Type-Options": "nosniff"}
    )


@router.get("/graph")
async def full_graph(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    """The graph view's default: the whole graph (every visible entity, including
    disconnected ones, + all relationship edges), centered on the "Me" entity.
    RLS-scoped, so firewalled entities and their edges never leak. An empty
    knowledge base returns an empty graph rather than 404."""
    return await get_analysis_repo(request).full_graph(ctx_for(principal))


@router.get("/review")
async def review_list(
    request: Request,
    principal: PrincipalDep,
    status: Annotated[str, Query()] = "open",
) -> dict[str, Any]:
    if status not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail="unknown status")
    items = await get_analysis_repo(request).list_review(ctx_for(principal), status)
    return {"items": items}


@router.get("/review/{item_id}/predicate-suggestions")
async def review_predicate_suggestions(
    item_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    """The weighted relation candidates the correct-in-place predicate picker
    offers for a held inference — computed on demand so any open card gets live
    suggestions. Embedder failures surface as an empty list (the picker falls
    back to manual entry); 404 only when the item is gone."""
    try:
        suggestions = await get_analysis_repo(request).predicate_suggestions(
            ctx_for(principal), item_id, embedder=get_embed_client(request)
        )
    except Exception:  # noqa: BLE001 — a flaky embedder must not break the picker
        return {"suggestions": []}
    if suggestions is None:
        raise HTTPException(status_code=404, detail="review item not found")
    return {"suggestions": suggestions}


class ResolveRequest(BaseModel):
    action: str = Field(min_length=1)
    payload: dict[str, Any] = {}


@router.post("/review/{item_id}/resolve")
async def resolve_review(
    item_id: str, body: ResolveRequest, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    repo = get_analysis_repo(request)
    try:
        item = await repo.resolve_review(ctx_for(principal), item_id, body.action, body.payload)
    except UnknownAction as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except AlreadyResolved:
        raise HTTPException(status_code=409, detail="review item is not open") from None
    if item is None:
        raise HTTPException(status_code=404, detail="review item not found")
    return item


class BatchDecision(BaseModel):
    id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    payload: dict[str, Any] = {}


class ResolveBatchRequest(BaseModel):
    decisions: list[BatchDecision] = Field(min_length=1, max_length=200)


@router.post("/review/resolve-batch")
async def resolve_review_batch(
    body: ResolveBatchRequest, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    """Bulk-apply the same-shaped per-item decisions in one transaction; the
    good ones commit and bad ones come back in `errors` (the UI rolls those
    rows back). Used by the inbox's select-and-approve / defer-all actions."""
    repo = get_analysis_repo(request)
    return await repo.resolve_review_batch(
        ctx_for(principal), [d.model_dump() for d in body.decisions]
    )


@router.post("/review/{item_id}/reopen")
async def reopen_review(item_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    """Full unwind: reverses the resolution's recorded graph effects and
    re-queues the item. Permanent distinct_from edges survive by doctrine;
    the response's reopen_note says so when one was kept."""
    repo = get_analysis_repo(request)
    try:
        item = await repo.reopen_review(ctx_for(principal), item_id)
    except AlreadyOpen:
        raise HTTPException(status_code=409, detail="review item is already open") from None
    if item is None:
        raise HTTPException(status_code=404, detail="review item not found")
    return item
