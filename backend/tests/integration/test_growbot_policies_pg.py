"""Migration 0268 against real Postgres: GrowBot's policy library is owner-only, and the repo's
SQL (add, list, rename, delete, the sidecar's JSON read) does what the api assumes.

The mandatory RLS isolation test for the new table (CLAUDE.md rule 3).
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.growbot.policies import SqlPolicyRepo
from tests.conftest import docker_available
from tests.integration.test_growbot_rls import NON_OWNERS
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

SYSTEM_OWNER = SessionContext(principal_kind="owner")
POLICY: dict[str, Any] = {
    "obs_size": 2,
    "mean": [0.0, 0.0],
    "std": [1.0, 1.0],
    "layers": [{"W": [[0.0, 0.0], [0.0, 0.0]], "b": [0.0, 0.0]}],
    "obs_spec": {
        "control_hz": 25,
        "segments": [{"channels": ["gyro_z", "cmd_yaw"], "frames": 1}],
        "cmd_yaw": {"meaning": "heading_error", "facing": "camera"},
    },
}


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    made = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(made, SYSTEM_OWNER) as session:
        await session.execute(text("DELETE FROM app.growbot_policies"))
    yield made
    await engine.dispose()


async def test_the_library_round_trips(maker: async_sessionmaker) -> None:
    repo = SqlPolicyRepo(maker)
    row = await repo.add(OWNER, "First walk", POLICY, 1234, "job1", "growbot_camera_walk")
    assert row.contract.facing == "camera" and row.contract.control_hz == 25
    assert [r.id for r in await repo.list(OWNER)] == [row.id]
    assert await repo.get_json(SYSTEM_OWNER, row.id) == POLICY
    assert await repo.exists(OWNER, row.id)
    renamed = await repo.rename(OWNER, row.id, "Brisk")
    assert renamed is not None and renamed.name == "Brisk" and renamed.bytes == 1234
    assert await repo.delete(OWNER, row.id)
    assert not await repo.delete(OWNER, row.id)
    assert await repo.get_json(OWNER, row.id) is None
    assert not await repo.exists(OWNER, row.id)


async def test_a_listing_reads_the_weights_only_as_present(maker: async_sessionmaker) -> None:
    """list() summarises a stand-in for the weights, which must still tell a row with empty
    ones apart: that row is left out, as summary() refuses it."""
    repo = SqlPolicyRepo(maker)
    good = await repo.add(OWNER, "good", POLICY, 1, "", "")
    async with scoped_session(maker, SYSTEM_OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.growbot_policies (name, source_job, source_spec, policy, bytes)"
                " VALUES ('hollow', '', '', CAST(:p AS jsonb), 1)"
            ),
            {"p": json.dumps({**POLICY, "layers": []})},
        )
    assert [r.id for r in await repo.list(OWNER)] == [good.id]


async def test_the_library_is_owner_only(maker: async_sessionmaker) -> None:
    repo = SqlPolicyRepo(maker)
    row = await repo.add(OWNER, "a", POLICY, 1, "", "")
    for ctx in NON_OWNERS:
        assert await repo.list(ctx) == [], ctx
        assert await repo.get_json(ctx, row.id) is None, ctx
        assert not await repo.exists(ctx, row.id), ctx
        assert await repo.rename(ctx, row.id, "x") is None, ctx
        assert not await repo.delete(ctx, row.id), ctx
        with pytest.raises(DBAPIError):
            await repo.add(ctx, "b", POLICY, 1, "", "")
    assert [r.name for r in await repo.list(OWNER)] == ["a"]


async def test_the_table_refuses_a_bad_name_or_a_non_object(maker: async_sessionmaker) -> None:
    repo = SqlPolicyRepo(maker)
    with pytest.raises(DBAPIError):
        await repo.add(OWNER, "", POLICY, 1, "", "")
    with pytest.raises(DBAPIError):
        await repo.add(OWNER, "x" * 61, POLICY, 1, "", "")
    async with scoped_session(maker, OWNER) as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO app.growbot_policies (name, policy, bytes)"
                    " VALUES ('x', '[1]'::jsonb, 1)"
                )
            )
