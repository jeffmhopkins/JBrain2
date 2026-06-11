"""Analysis read/resolve endpoints: per-note extraction view, entity pages,
and the review inbox. Owner-only is implicit pre-P7: every query runs on the
principal's RLS context, and only the owner holds a session today.

The response shapes are a frozen contract with the frontend — change them
only with a coordinated frontend PR.
"""

from typing import Annotated, Any, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from jbrain.analysis.repo import (
    REVIEW_STATUSES,
    AlreadyOpen,
    AlreadyResolved,
    SqlAnalysisRepo,
    UnknownAction,
)
from jbrain.api.deps import PrincipalDep
from jbrain.api.notes import ctx_for

router = APIRouter()


def get_analysis_repo(request: Request) -> SqlAnalysisRepo:
    return cast(SqlAnalysisRepo, request.app.state.analysis_repo)


@router.get("/notes/{note_id}/analysis")
async def note_analysis(note_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    view = await get_analysis_repo(request).note_analysis_view(ctx_for(principal), note_id)
    if view is None:
        raise HTTPException(status_code=404, detail="note not found")
    return view


@router.get("/entities/{entity_id}")
async def entity_detail(
    entity_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    view = await get_analysis_repo(request).entity_view(ctx_for(principal), entity_id)
    if view is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return view


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
