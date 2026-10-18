"""Migration 0252 against real Postgres: who can read a JShare item is decided in RLS.

The owner reads and writes every item. A visitor runs under `jshare_context(slug)` and
reads exactly the item that slug names, nothing else in the table or the schema, and can
write nothing. Deleting the row is what takes a link down.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.blob_refs import blob_referenced
from jbrain.db.session import SessionContext, jshare_context, scoped_session
from jbrain.jshare import store
from jbrain.jshare.content import new_slug
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


async def _owner_ctx(maker: async_sessionmaker) -> SessionContext:
    # principal_id FKs to app.principals, so the owner context carries a real owner.
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _add(maker: async_sessionmaker, ctx: SessionContext, sha: str) -> store.ShareItem:
    return await store.add(
        maker,
        ctx,
        slug=new_slug(),
        title="t",
        kind="image",
        filename="a.png",
        media_type="image/png",
        size_bytes=1,
        blob_sha256=sha,
        site_root="",
    )


async def _visible(maker: async_sessionmaker, slug: str) -> set[str]:
    async with scoped_session(maker, jshare_context(slug)) as s:
        rows = (await s.execute(text("SELECT slug FROM app.jshare_items"))).all()
    return {r.slug for r in rows}


async def test_a_slug_reads_only_its_own_item(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    a = await _add(maker, owner, "a" * 64)
    b = await _add(maker, owner, "b" * 64)

    got = await store.fetch_public(maker, a.slug)
    assert got is not None and got.id == a.id
    # RLS, not the WHERE clause, is the gate: an unfiltered read still sees one row.
    assert await _visible(maker, a.slug) == {a.slug}
    assert await _visible(maker, b.slug) == {b.slug}
    assert await _visible(maker, "") == set()
    assert await _visible(maker, "z" * 26) == set()
    assert {i.id for i in await store.list_items(maker, owner)} >= {a.id, b.id}


async def test_a_visitor_reads_nothing_else(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    item = await _add(maker, owner, "c" * 64)
    async with scoped_session(maker, jshare_context(item.slug)) as s:
        for table in ("app.notes", "app.attachments", "app.principals", "app.jlaunch_share_links"):
            n = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            assert n == 0, f"a JShare visitor must not read {table}"


async def test_a_visitor_writes_nothing(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    item = await _add(maker, owner, "d" * 64)
    async with scoped_session(maker, jshare_context(item.slug)) as s:
        updated = await s.execute(
            text("UPDATE app.jshare_items SET title = 'pwned' WHERE slug = :s RETURNING id"),
            {"s": item.slug},
        )
        assert updated.all() == []
        deleted = await s.execute(text("DELETE FROM app.jshare_items RETURNING id"))
        assert deleted.all() == []
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, jshare_context(item.slug)) as s:
            await s.execute(
                text(
                    "INSERT INTO app.jshare_items (slug, title, kind, filename, media_type,"
                    " size_bytes, blob_sha256, principal_id)"
                    " VALUES (:slug, 't', 'file', 'f', 'x', 1, 'e', cast(:pid AS uuid))"
                ),
                {"slug": new_slug(), "pid": owner.principal_id},
            )
    assert (await store.fetch_public(maker, item.slug)).title == "t"  # type: ignore[union-attr]


async def test_a_non_owner_session_sees_no_items(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    await _add(maker, owner, "f" * 64)
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    async with scoped_session(maker, token) as s:
        n = (await s.execute(text("SELECT count(*) FROM app.jshare_items"))).scalar_one()
    assert n == 0


async def test_delete_takes_the_link_down_and_releases_the_blob(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    sha = "1" * 64
    item = await _add(maker, owner, sha)
    async with scoped_session(maker, owner) as s:
        assert await blob_referenced(s, sha)
    assert await store.delete(maker, owner, item.id) is not None
    assert await store.fetch_public(maker, item.slug) is None
    assert not await store.blob_in_use(maker, owner, sha)
    assert await store.delete(maker, owner, item.id) is None
    assert await store.delete(maker, owner, "not-a-uuid") is None


async def test_views_are_counted_and_rename_sticks(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    item = await _add(maker, owner, "2" * 64)
    await store.count_view(maker, item.id)
    await store.count_view(maker, item.id)
    renamed = await store.rename(maker, owner, item.id, "Beach")
    assert renamed is not None
    assert (renamed.title, renamed.view_count) == ("Beach", 2)
    assert renamed.last_viewed_at is not None
    assert await store.rename(maker, owner, "not-a-uuid", "x") is None
