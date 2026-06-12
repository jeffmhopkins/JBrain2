"""Lists API: the owner managing their own lists directly from the PWA.

Owner-only. Lists are the owner's own data (not citable truth), so the owner
creates, reorders, edits, and checks them directly — the agent's `list_card`
renders a checklist, the Lists screen is the full manager, and every mutation
lands here. RLS still scopes every query, and a missing/out-of-scope id is a 404
(never a leak).
"""

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from jbrain.api.deps import PrincipalDep, owner_only
from jbrain.api.notes import ctx_for
from jbrain.lists.repo import SqlListsRepo
from jbrain.lists.service import ListInfo, ListItemInfo, UnknownDomain

router = APIRouter(prefix="/lists", dependencies=[Depends(owner_only)])


def get_lists_repo(request: Request) -> SqlListsRepo:
    return cast(SqlListsRepo, request.app.state.lists_repo)


class ListItemOut(BaseModel):
    id: str
    body: str
    checked: bool

    @classmethod
    def of(cls, item: ListItemInfo) -> "ListItemOut":
        return cls(id=item.id, body=item.body, checked=item.checked)


class ListOut(BaseModel):
    id: str
    title: str
    domain: str
    archived: bool
    items: list[ListItemOut]

    @classmethod
    def of(cls, info: ListInfo) -> "ListOut":
        return cls(
            id=info.id,
            title=info.title,
            domain=info.domain,
            archived=info.archived,
            items=[ListItemOut.of(i) for i in info.items],
        )


class ListCreate(BaseModel):
    title: str
    domain: str = "general"


class ListPatch(BaseModel):
    title: str | None = None
    archived: bool | None = None


class ItemCreate(BaseModel):
    body: str


class ItemPatch(BaseModel):
    checked: bool | None = None
    body: str | None = None


class Reorder(BaseModel):
    item_ids: list[str]


@router.get("")
async def list_lists(request: Request, principal: PrincipalDep) -> list[ListOut]:
    repo = get_lists_repo(request)
    return [ListOut.of(i) for i in await repo.list_lists(ctx_for(principal))]


@router.get("/{list_id}")
async def get_list(request: Request, principal: PrincipalDep, list_id: str) -> ListOut:
    info = await get_lists_repo(request).get_list(ctx_for(principal), list_id)
    if info is None:
        raise HTTPException(status_code=404, detail="no such list")
    return ListOut.of(info)


@router.post("", status_code=201)
async def create_list(request: Request, principal: PrincipalDep, body: ListCreate) -> ListOut:
    try:
        info = await get_lists_repo(request).create_list(
            ctx_for(principal), domain=body.domain, title=body.title
        )
    except UnknownDomain as exc:
        raise HTTPException(status_code=422, detail="unknown domain") from exc
    return ListOut.of(info)


@router.patch("/{list_id}")
async def patch_list(
    request: Request, principal: PrincipalDep, list_id: str, body: ListPatch
) -> ListOut:
    repo = get_lists_repo(request)
    ctx = ctx_for(principal)
    info: ListInfo | None = None
    if body.title is not None:
        info = await repo.rename_list(ctx, list_id, body.title)
    if body.archived is not None:
        info = await repo.archive_list(ctx, list_id, archived=body.archived)
    if info is None:
        raise HTTPException(status_code=404, detail="no such list")
    return ListOut.of(info)


@router.delete("/{list_id}", status_code=204)
async def delete_list(request: Request, principal: PrincipalDep, list_id: str) -> Response:
    if not await get_lists_repo(request).delete_list(ctx_for(principal), list_id):
        raise HTTPException(status_code=404, detail="no such list")
    return Response(status_code=204)


@router.post("/{list_id}/items", status_code=201)
async def add_item(
    request: Request, principal: PrincipalDep, list_id: str, body: ItemCreate
) -> ListItemOut:
    item = await get_lists_repo(request).add_item(ctx_for(principal), list_id, body.body)
    if item is None:
        raise HTTPException(status_code=404, detail="no such list")
    return ListItemOut.of(item)


@router.patch("/{list_id}/order", status_code=204)
async def reorder(
    request: Request, principal: PrincipalDep, list_id: str, body: Reorder
) -> Response:
    if not await get_lists_repo(request).reorder_items(ctx_for(principal), list_id, body.item_ids):
        raise HTTPException(status_code=404, detail="no such list")
    return Response(status_code=204)


@router.patch("/items/{item_id}")
async def patch_item(
    request: Request, principal: PrincipalDep, item_id: str, body: ItemPatch
) -> ListItemOut:
    """Check/uncheck and/or rename an item. 404 when the item isn't the owner's."""
    repo = get_lists_repo(request)
    ctx = ctx_for(principal)
    item: ListItemInfo | None = None
    if body.body is not None:
        item = await repo.rename_item(ctx, item_id, body.body)
    if body.checked is not None:
        item = await repo.set_item_checked(ctx, item_id, checked=body.checked)
    if item is None:
        raise HTTPException(status_code=404, detail="no such item")
    return ListItemOut.of(item)


@router.delete("/items/{item_id}", status_code=204)
async def remove_item(request: Request, principal: PrincipalDep, item_id: str) -> Response:
    if not await get_lists_repo(request).remove_item(ctx_for(principal), item_id):
        raise HTTPException(status_code=404, detail="no such item")
    return Response(status_code=204)
