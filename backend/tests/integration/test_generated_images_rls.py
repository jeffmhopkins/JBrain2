"""Migration 0077 against real Postgres: `generated_images` is owner-only (CLAUDE.md rule 3).

The mandatory per-new-table RLS isolation test for the image-gen chat-artifact table. The
owner round-trips an insert/select via `GeneratedImageRepo`; a non-owner (capability-token)
principal sees ZERO rows and cannot insert (the owner WITH CHECK blocks the write); and the
table is immutable to the app role (no UPDATE grant), asserted directly.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.images import GeneratedImageRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A non-owner principal: a capability token with no owner identity — app.is_owner() is false.
NON_OWNER = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def test_owner_insert_select_roundtrips(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    repo = GeneratedImageRepo()

    async with scoped_session(maker, owner) as session:
        row = await repo.insert(
            session,
            blob_sha256="ab" * 32,
            kind="generate",
            model="qwen-image-2512",
            prompt="a red bicycle",
            source_sha256=None,
            width=1024,
            height=1024,
            steps=20,
            seed=4242424242,
        )
        image_id = str(row.id)

    async with scoped_session(maker, owner) as session:
        fetched = await repo.get(session, image_id)
    assert fetched is not None
    assert fetched.kind == "generate"
    assert fetched.prompt == "a red bicycle"
    assert fetched.seed == 4242424242  # bigint round-trips
    assert fetched.source_sha256 is None


async def test_non_owner_sees_no_rows_and_cannot_insert(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    repo = GeneratedImageRepo()

    # Baseline so the assertions are independent of rows other tests left in the
    # shared module DB (the non-owner count is absolute — RLS hides every row).
    async with scoped_session(maker, owner) as session:
        baseline = (
            await session.execute(text("SELECT count(*) FROM app.generated_images"))
        ).scalar() or 0

    async with scoped_session(maker, owner) as session:
        await repo.insert(
            session,
            blob_sha256="cd" * 32,
            kind="edit",
            model="qwen-image-edit",
            prompt="make it blue",
            source_sha256="ab" * 32,
            width=768,
            height=768,
            steps=15,
            seed=7,
        )

    # A non-owner principal sees zero rows — RLS hides the owner's artifacts entirely.
    async with scoped_session(maker, NON_OWNER) as session:
        count = (await session.execute(text("SELECT count(*) FROM app.generated_images"))).scalar()
    assert count == 0

    # …and cannot write: the owner WITH CHECK rejects a non-owner insert.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, NON_OWNER) as session:
            await repo.insert(
                session,
                blob_sha256="ef" * 32,
                kind="generate",
                model="qwen-image-2512",
                prompt="sneaky",
                source_sha256=None,
                width=512,
                height=512,
                steps=10,
                seed=1,
            )

    # The failed write left nothing behind: only the one owner row was added.
    async with scoped_session(maker, owner) as session:
        owner_count = (
            await session.execute(text("SELECT count(*) FROM app.generated_images"))
        ).scalar()
    assert owner_count == baseline + 1


async def test_provenanced_rows_owner_only_hidden_from_gallery_but_resolvable(
    maker: async_sessionmaker,
) -> None:
    """A grabbed/fetched still (migration 0139) is owner-only like any row, is EXCLUDED from
    the gallery `list()` (it is a transient chat image, not a render the owner made), yet is
    still resolvable by id so the in-chat tools reach it."""
    owner = await _owner(maker)
    repo = GeneratedImageRepo()

    async with scoped_session(maker, owner) as session:
        row = await repo.insert(
            session,
            blob_sha256="a1" * 32,
            kind="generate",
            model="web_fetch",
            prompt="https://intellijel.com/metropolis.jpg",
            source_sha256=None,
            width=800,
            height=800,
            steps=0,
            seed=0,
            provenance="web_fetch",
        )
        image_id = str(row.id)

    async with scoped_session(maker, owner) as session:
        # Resolvable by id (what analyze_image/compare_images use)…
        fetched = await repo.get(session, image_id)
        assert fetched is not None and fetched.provenance == "web_fetch"
        # …but never in the gallery listing.
        gallery = await repo.list(session, limit=1000)
    assert all(r.provenance is None for r in gallery)
    assert image_id not in {str(r.id) for r in gallery}

    # Owner-only: a non-owner sees nothing, provenance notwithstanding.
    async with scoped_session(maker, NON_OWNER) as session:
        count = (await session.execute(text("SELECT count(*) FROM app.generated_images"))).scalar()
    assert count == 0


async def test_rows_are_immutable_no_update_grant(maker: async_sessionmaker) -> None:
    """Rows are generation provenance: the app role has SELECT/INSERT/DELETE but NO UPDATE
    grant (migration 0077), so even the owner cannot mutate a recorded image."""
    owner = await _owner(maker)
    repo = GeneratedImageRepo()

    async with scoped_session(maker, owner) as session:
        row = await repo.insert(
            session,
            blob_sha256="11" * 32,
            kind="generate",
            model="qwen-image-2512",
            prompt="immutable",
            source_sha256=None,
            width=1024,
            height=1024,
            steps=20,
            seed=99,
        )
        image_id = str(row.id)

    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, owner) as session:
            await session.execute(
                text("UPDATE app.generated_images SET prompt = 'x' WHERE id = cast(:id AS uuid)"),
                {"id": image_id},
            )
