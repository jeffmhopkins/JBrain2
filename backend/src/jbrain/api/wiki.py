"""GET /api/wiki/landing and /api/wiki/{id} — the read side of the machine-written wiki.

Owner-only is implicit pre-P7 (every query runs on the principal's RLS context, only the owner
holds a session today). The response shapes are a frozen contract with the frontend reader/landing
(WikiArticleOut / WikiLandingOut in frontend/src/api/client.ts) — change them only with a
coordinated frontend PR. `landing` is declared before `{id}` so the static path wins the match.
"""

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request

from jbrain.api.deps import PrincipalDep
from jbrain.api.notes import ctx_for
from jbrain.wiki.readstore import WikiReadStore

router = APIRouter()


def get_wiki_read_store(request: Request) -> WikiReadStore:
    return cast(WikiReadStore, request.app.state.wiki_read_store)


@router.get("/wiki/landing")
async def wiki_landing(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return await get_wiki_read_store(request).get_landing(ctx_for(principal))


@router.get("/wiki/{article_id}")
async def wiki_article(
    article_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    article = await get_wiki_read_store(request).get_article(ctx_for(principal), article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="article not found")
    return article
