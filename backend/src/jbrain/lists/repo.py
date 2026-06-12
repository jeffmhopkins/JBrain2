"""SQL lists repository. Every query runs on an RLS-scoped session, so domain
filtering (and the owner-only firewall) is enforced by Postgres, not here."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from jbrain.db.session import SessionContext, scoped_session
from jbrain.lists.service import ListInfo, ListItemInfo, UnknownDomain
from jbrain.models.lists import List, ListItem


def _item_info(i: ListItem) -> ListItemInfo:
    return ListItemInfo(
        id=str(i.id),
        body=i.body,
        checked=i.checked_at is not None,
        position=i.position,
        source_note_id=str(i.source_note_id) if i.source_note_id is not None else None,
        created_at=i.created_at,
    )


def _list_info(lst: List) -> ListInfo:
    return ListInfo(
        id=str(lst.id),
        domain=lst.domain_code,
        title=lst.title,
        archived=lst.archived_at is not None,
        created_at=lst.created_at,
        updated_at=lst.updated_at,
        items=[_item_info(i) for i in lst.items],
    )


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


class SqlListsRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def create_list(self, ctx: SessionContext, *, domain: str, title: str) -> ListInfo:
        try:
            async with scoped_session(self._maker, ctx) as session:
                lst = List(domain_code=domain, title=title, principal_id=_principal(ctx))
                session.add(lst)
                await session.flush()
                await session.refresh(lst, ["items"])
                return _list_info(lst)
        except IntegrityError as exc:
            raise UnknownDomain(domain) from exc

    async def list_lists(
        self, ctx: SessionContext, *, include_archived: bool = False
    ) -> list[ListInfo]:
        async with scoped_session(self._maker, ctx) as session:
            query = (
                select(List)
                .options(selectinload(List.items))
                .order_by(List.updated_at.desc(), List.id.desc())
            )
            if not include_archived:
                query = query.where(List.archived_at.is_(None))
            rows = (await session.execute(query)).scalars().all()
            return [_list_info(lst) for lst in rows]

    async def get_list(self, ctx: SessionContext, list_id: str) -> ListInfo | None:
        lid = _as_uuid(list_id)
        if lid is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            lst = (
                await session.execute(
                    select(List).options(selectinload(List.items)).where(List.id == lid)
                )
            ).scalar_one_or_none()
            return _list_info(lst) if lst is not None else None

    async def rename_list(self, ctx: SessionContext, list_id: str, title: str) -> ListInfo | None:
        lid = _as_uuid(list_id)
        if lid is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                update(List)
                .where(List.id == lid)
                .values(title=title, updated_at=func.now())
                .returning(List.id)
            )
            if result.scalar_one_or_none() is None:
                return None
        return await self.get_list(ctx, list_id)

    async def archive_list(
        self, ctx: SessionContext, list_id: str, *, archived: bool
    ) -> ListInfo | None:
        lid = _as_uuid(list_id)
        if lid is None:
            return None
        when = datetime.now(UTC) if archived else None
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                update(List)
                .where(List.id == lid)
                .values(archived_at=when, updated_at=func.now())
                .returning(List.id)
            )
            if result.scalar_one_or_none() is None:
                return None
        return await self.get_list(ctx, list_id)

    async def add_item(
        self,
        ctx: SessionContext,
        list_id: str,
        body: str,
        *,
        source_note_id: str | None = None,
    ) -> ListItemInfo | None:
        lid = _as_uuid(list_id)
        if lid is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            # RLS makes an out-of-scope list invisible, so the parent check (and
            # the next-position read) sees nothing and we bail rather than orphan.
            owner = (
                await session.execute(select(List.id).where(List.id == lid))
            ).scalar_one_or_none()
            if owner is None:
                return None
            next_pos = (
                await session.execute(
                    select(func.coalesce(func.max(ListItem.position), -1) + 1).where(
                        ListItem.list_id == lid
                    )
                )
            ).scalar_one()
            item = ListItem(
                list_id=lid,
                body=body,
                position=next_pos,
                source_note_id=_as_uuid(source_note_id) if source_note_id else None,
            )
            session.add(item)
            await session.execute(update(List).where(List.id == lid).values(updated_at=func.now()))
            await session.flush()
            await session.refresh(item)
            return _item_info(item)

    async def set_item_checked(
        self, ctx: SessionContext, item_id: str, *, checked: bool
    ) -> ListItemInfo | None:
        iid = _as_uuid(item_id)
        if iid is None:
            return None
        when = datetime.now(UTC) if checked else None
        async with scoped_session(self._maker, ctx) as session:
            item = (
                await session.execute(select(ListItem).where(ListItem.id == iid))
            ).scalar_one_or_none()
            if item is None:
                return None
            item.checked_at = when
            await session.execute(
                update(List).where(List.id == item.list_id).values(updated_at=func.now())
            )
            await session.flush()
            await session.refresh(item)
            return _item_info(item)

    async def remove_item(self, ctx: SessionContext, item_id: str) -> bool:
        iid = _as_uuid(item_id)
        if iid is None:
            return False
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                delete(ListItem).where(ListItem.id == iid).returning(ListItem.id)
            )
            return result.scalar_one_or_none() is not None

    async def rename_item(
        self, ctx: SessionContext, item_id: str, body: str
    ) -> ListItemInfo | None:
        iid = _as_uuid(item_id)
        if iid is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            item = (
                await session.execute(select(ListItem).where(ListItem.id == iid))
            ).scalar_one_or_none()
            if item is None:
                return None
            item.body = body
            await session.execute(
                update(List).where(List.id == item.list_id).values(updated_at=func.now())
            )
            await session.flush()
            await session.refresh(item)
            return _item_info(item)

    async def reorder_items(self, ctx: SessionContext, list_id: str, item_ids: list[str]) -> bool:
        """Set each item's position to its index in `item_ids`. Items not in the
        list are skipped (the WHERE pins them to the parent); False when the list
        isn't in scope."""
        lid = _as_uuid(list_id)
        if lid is None:
            return False
        async with scoped_session(self._maker, ctx) as session:
            owner = (
                await session.execute(select(List.id).where(List.id == lid))
            ).scalar_one_or_none()
            if owner is None:
                return False
            for pos, raw in enumerate(item_ids):
                iid = _as_uuid(raw)
                if iid is None:
                    continue
                await session.execute(
                    update(ListItem)
                    .where(ListItem.id == iid, ListItem.list_id == lid)
                    .values(position=pos)
                )
            await session.execute(update(List).where(List.id == lid).values(updated_at=func.now()))
            return True

    async def delete_list(self, ctx: SessionContext, list_id: str) -> bool:
        lid = _as_uuid(list_id)
        if lid is None:
            return False
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(delete(List).where(List.id == lid).returning(List.id))
            return result.scalar_one_or_none() is not None


def _principal(ctx: SessionContext) -> uuid.UUID:
    """The owner principal id stamped on a new list (RLS already proved owner)."""
    if ctx.principal_id is None:
        raise ValueError("a list write needs an owner principal in context")
    return uuid.UUID(ctx.principal_id)
