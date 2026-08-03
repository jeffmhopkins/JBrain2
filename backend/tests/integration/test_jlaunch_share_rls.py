"""Migrations 0152/0153 against real Postgres: jlaunch run visibility is enforced in RLS,
not app code.

The security keystone. The owner-only `jlaunch_runs` mirror is invisible to any non-owner
EXCEPT a public results-share visitor, who runs under `jlaunch_share_context` — a non-owner,
empty-scope principal pinned to one share-link id. The `jlaunch_runs_share` policy is the ONLY
grant it earns: it reads exactly the pinned link's run and nothing else, with revocation
re-checked at the data layer and no write anywhere.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, jlaunch_share_context, scoped_session
from jbrain.external.jlaunch_shares import (
    fetch_shared_run,
    list_shares,
    mint_share,
    resolve_share,
    revoke_share,
)
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
    # jlaunch_share_links.principal_id FKs to app.principals, so the owner ctx must carry a
    # REAL owner principal — rotate a key, read it back.
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _insert_run(maker: async_sessionmaker, ctx: SessionContext, run_id: str) -> str:
    async with scoped_session(maker, ctx) as s:
        await s.execute(
            text(
                "INSERT INTO app.jlaunch_runs (id, spec_name, title, state, verify_ok)"
                " VALUES (:id, 'erdos_straus_1e12', 'census', 'succeeded', true)"
            ),
            {"id": run_id},
        )
    return run_id


async def _visible_run_ids(maker: async_sessionmaker, link_id: str) -> set[str]:
    async with scoped_session(maker, jlaunch_share_context(link_id)) as s:
        rows = (await s.execute(text("SELECT id FROM app.jlaunch_runs"))).all()
    return {str(r.id) for r in rows}


async def test_link_reads_only_its_run(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    run_a = await _insert_run(maker, owner, "runa1234")
    run_b = await _insert_run(maker, owner, "runb5678")
    secret, link = await mint_share(maker, owner, run_id=run_a, label="A")

    # The token resolves (public_read policy) to the live link id; the read is pinned to it.
    assert await resolve_share(maker, secret) == link.id
    got = await fetch_shared_run(maker, link.id)
    assert got is not None and got.id == run_a

    # RLS, not a WHERE clause, is the gate: pinned to link A the ONLY visible run is A.
    assert await _visible_run_ids(maker, link.id) == {run_a}
    assert run_b not in await _visible_run_ids(maker, link.id)


async def test_share_context_leaks_nothing_else(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    run = await _insert_run(maker, owner, "runc0001")
    _, link = await mint_share(maker, owner, run_id=run, label="A")
    # Seed an owner-only jcode session and a health note — neither may be seen by a share
    # visitor. (A second jlaunch run is covered by the pin test above.)
    async with scoped_session(maker, owner) as s:
        await s.execute(text("INSERT INTO app.jcode_sessions (id, repo) VALUES ('sess9', 'r')"))
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, 'health', 'BP 118/76')"
            ),
            {"id": str(uuid.uuid4()), "cid": f"jl-{uuid.uuid4().hex[:12]}"},
        )

    async with scoped_session(maker, jlaunch_share_context(link.id)) as s:
        for table in ("app.jcode_sessions", "app.notes"):
            n = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            assert n == 0, f"share context must not read {table}"


async def test_revoked_link_reads_nothing(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    run = await _insert_run(maker, owner, "rund0002")
    secret, link = await mint_share(maker, owner, run_id=run, label="A")
    assert await revoke_share(maker, owner, link.id) is True

    assert await resolve_share(maker, secret) is None  # token dead
    assert await _visible_run_ids(maker, link.id) == set()  # policy re-checks revoked_at
    assert await fetch_shared_run(maker, link.id) is None
    assert await revoke_share(maker, owner, link.id) is False  # already revoked -> no-op


async def test_no_pin_and_write_denial(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    run = await _insert_run(maker, owner, "rune0003")
    _, link = await mint_share(maker, owner, run_id=run, label="A")
    # An unpinned (empty principal_id) share context reads zero runs and does NOT raise — the
    # policy compares the link id as text, so an empty pin matches no row (never a cast error).
    assert await _visible_run_ids(maker, "") == set()

    # Provably read-only: an insert under the pinned share context is refused by RLS.
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, jlaunch_share_context(link.id)) as s:
            await s.execute(
                text(
                    "INSERT INTO app.jlaunch_share_links (token_hash, run_id, principal_id)"
                    " VALUES (:h, :rid, cast(:pid AS uuid))"
                ),
                {"h": uuid.uuid4().hex, "rid": run, "pid": owner.principal_id},
            )


async def test_non_owner_cannot_read_runs_or_share_rows(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    run = await _insert_run(maker, owner, "runf0004")
    _, link = await mint_share(maker, owner, run_id=run, label="A")
    assert link.id in {s.id for s in await list_shares(maker, owner, run)}

    # A generic non-owner token (no jlaunch_share auth context) sees no runs and no share rows.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    async with scoped_session(maker, token) as s:
        runs = (await s.execute(text("SELECT count(*) FROM app.jlaunch_runs"))).scalar_one()
        links = (await s.execute(text("SELECT count(*) FROM app.jlaunch_share_links"))).scalar_one()
    assert runs == 0
    assert links == 0
