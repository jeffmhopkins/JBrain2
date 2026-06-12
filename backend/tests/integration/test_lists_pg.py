"""Migration 0022 against real Postgres: the lists repo round-trip.

Lists are owner-managed structured records — created, added to, checked off,
renamed, and archived directly (no Proposal round-trip). The firewall is RLS
(proven in test_lists_rls.py); this exercises the repo's behavior.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import scoped_session
from jbrain.lists.repo import SqlListsRepo
from jbrain.lists.service import UnknownDomain
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner_principal(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


async def test_list_round_trip(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    ctx = read_context(pid, ("general",))
    repo = SqlListsRepo(maker)

    lst = await repo.create_list(ctx, domain="general", title="Groceries")
    assert lst.title == "Groceries" and lst.total_count == 0 and not lst.archived

    eggs = await repo.add_item(ctx, lst.id, "eggs")
    milk = await repo.add_item(ctx, lst.id, "milk")
    assert eggs is not None and milk is not None
    assert milk.position == eggs.position + 1  # appended in order

    got = await repo.get_list(ctx, lst.id)
    assert got is not None
    assert [i.body for i in got.items] == ["eggs", "milk"]
    assert got.open_count == 2 and got.total_count == 2

    checked = await repo.set_item_checked(ctx, eggs.id, checked=True)
    assert checked is not None and checked.checked
    got = await repo.get_list(ctx, lst.id)
    assert got is not None and got.open_count == 1 and got.total_count == 2

    assert await repo.remove_item(ctx, milk.id) is True
    got = await repo.get_list(ctx, lst.id)
    assert got is not None and [i.body for i in got.items] == ["eggs"]

    renamed = await repo.rename_list(ctx, lst.id, "Food")
    assert renamed is not None and renamed.title == "Food"

    archived = await repo.archive_list(ctx, lst.id, archived=True)
    assert archived is not None and archived.archived
    # An archived list drops from the default view but survives include_archived.
    assert lst.id not in [x.id for x in await repo.list_lists(ctx)]
    assert lst.id in [x.id for x in await repo.list_lists(ctx, include_archived=True)]


async def test_reorder_rename_and_delete(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    ctx = read_context(pid, ("general",))
    repo = SqlListsRepo(maker)
    lst = await repo.create_list(ctx, domain="general", title="Trip")
    a = await repo.add_item(ctx, lst.id, "socks")
    b = await repo.add_item(ctx, lst.id, "shirts")
    c = await repo.add_item(ctx, lst.id, "shoes")
    assert a and b and c

    # Reorder reverses the list; items come back in the new position order.
    assert await repo.reorder_items(ctx, lst.id, [c.id, b.id, a.id]) is True
    got = await repo.get_list(ctx, lst.id)
    assert got is not None and [i.body for i in got.items] == ["shoes", "shirts", "socks"]

    # Rename an item in place.
    renamed = await repo.rename_item(ctx, a.id, "wool socks")
    assert renamed is not None and renamed.body == "wool socks"

    # Delete the whole list — it and its items are gone.
    assert await repo.delete_list(ctx, lst.id) is True
    assert await repo.get_list(ctx, lst.id) is None


async def test_missing_ids_return_none_not_error(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    ctx = read_context(pid, ("general",))
    repo = SqlListsRepo(maker)
    assert await repo.get_list(ctx, "not-a-uuid") is None
    assert await repo.add_item(ctx, "00000000-0000-0000-0000-000000000000", "x") is None
    assert await repo.set_item_checked(ctx, "nope", checked=True) is None
    assert await repo.remove_item(ctx, "nope") is False
    assert await repo.rename_item(ctx, "nope", "x") is None
    assert await repo.reorder_items(ctx, "nope", []) is False
    assert await repo.delete_list(ctx, "nope") is False


async def test_create_list_rejects_unknown_domain(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    # The session claims the bogus scope (so RLS WITH CHECK passes), but the FK to
    # app.domains rejects it — surfaced as UnknownDomain, never a raw 500.
    ctx = read_context(pid, ("bogus",))
    with pytest.raises(UnknownDomain):
        await SqlListsRepo(maker).create_list(ctx, domain="bogus", title="x")
