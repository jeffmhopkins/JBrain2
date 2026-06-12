"""Lists API: the owner acting on their own lists directly from the PWA.

Owner-only. Lists are the owner's own data (not citable truth), so the owner
toggles items directly — the agent's `list_card` renders a checklist, and a
checkbox tap lands here. RLS still scopes every query, and a missing/out-of-scope
item is a 404 (never a leak).
"""

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jbrain.api.deps import PrincipalDep, owner_only
from jbrain.api.notes import ctx_for
from jbrain.lists.repo import SqlListsRepo
from jbrain.lists.service import ListItemInfo

router = APIRouter(prefix="/lists", dependencies=[Depends(owner_only)])


def get_lists_repo(request: Request) -> SqlListsRepo:
    return cast(SqlListsRepo, request.app.state.lists_repo)


class ItemCheck(BaseModel):
    checked: bool


class ListItemOut(BaseModel):
    id: str
    body: str
    checked: bool

    @classmethod
    def of(cls, item: ListItemInfo) -> "ListItemOut":
        return cls(id=item.id, body=item.body, checked=item.checked)


@router.patch("/items/{item_id}")
async def check_item(
    request: Request, principal: PrincipalDep, item_id: str, body: ItemCheck
) -> ListItemOut:
    """Check or uncheck a list item. 404 when the item isn't the owner's."""
    item = await get_lists_repo(request).set_item_checked(
        ctx_for(principal), item_id, checked=body.checked
    )
    if item is None:
        raise HTTPException(status_code=404, detail="no such item")
    return ListItemOut.of(item)
