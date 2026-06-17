"""GET /api/search — hybrid passage-first search (docs/DESIGN.md "Search").

`degraded: true` + keyword-only results when the embed container is down
(the UI's amber banner); `match` is the per-result provenance badge.
"""

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from jbrain.api.deps import PrincipalDep
from jbrain.api.notes import ctx_for
from jbrain.search.service import SearchResponse, SearchService, WikiSearchResult

router = APIRouter()


def get_search_service(request: Request) -> SearchService:
    return cast(SearchService, request.app.state.search_service)


class SearchResultOut(BaseModel):
    kind: Literal["note"] = "note"
    note_id: str
    chunk_id: str
    snippet: str
    match: str  # 'semantic' | 'keyword' | 'both' — the UI's match badge
    score: float
    domain: str
    destination: str | None
    created_at: datetime
    body_preview: str
    attachment_count: int
    source_kind: str
    source_anchor: str | None


class WikiSearchResultOut(BaseModel):
    kind: Literal["wiki"] = "wiki"
    article_id: str
    title: str
    blurb: str
    entity_kind: str
    domain: str
    snippet: str
    match: str
    score: float


class SearchOut(BaseModel):
    degraded: bool
    # A discriminated union: note passages and wiki articles, `kind` telling them apart.
    results: list[SearchResultOut | WikiSearchResultOut]


def search_out(resp: SearchResponse) -> SearchOut:
    return SearchOut(
        degraded=resp.degraded,
        results=[
            WikiSearchResultOut(**asdict(r))
            if isinstance(r, WikiSearchResult)
            else SearchResultOut(**asdict(r))
            for r in resp.results
        ],
    )


@router.get("/search")
async def search(
    request: Request,
    principal: PrincipalDep,
    q: Annotated[str, Query(min_length=1)],
    domain: str | None = None,
    limit: int = 20,
) -> SearchOut:
    limit = max(1, min(limit, 100))
    service = get_search_service(request)
    return search_out(await service.search(ctx_for(principal), q, domain, limit))
