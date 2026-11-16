"""Migration 0268 against real Postgres: GrowBot's policy library is owner-only, and the repo's
SQL (add, list, rename, delete, the sidecar's JSON read) does what the api assumes.

The mandatory RLS isolation test for the new table (CLAUDE.md rule 3).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.growbot.policies import PolicyError, SqlPolicyRepo
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


async def test_the_switches_keep_one_active_per_family_and_motion(
    maker: async_sessionmaker,
) -> None:
    repo = SqlPolicyRepo(maker)
    a = await repo.add(OWNER, "A", POLICY, 10, "job1", "growbot_camera_walk")
    b = await repo.add(OWNER, "B", POLICY, 10, "job2", "growbot_camera_walk")
    t = await repo.add(OWNER, "T", POLICY, 10, "job3", "growbot_camera_walk")
    c = await repo.add(OWNER, "C", POLICY, 10, "job4", "growbot_look")
    assert (a.enabled, a.motion, a.active) == (True, "walk", False)
    await repo.update(OWNER, a.id, active=True)
    await repo.update(OWNER, c.id, active=True)
    # Picking the family's turn keeps its walk's pick.
    await repo.update(OWNER, t.id, motion="turn", active=True)
    picked = {r.name: r.active for r in await repo.list(OWNER)}
    assert picked == {"A": True, "B": False, "T": True, "C": True}
    # Picking B unpicks A, its family's walk; renamed and switched in the same transaction.
    got = await repo.update(OWNER, b.id, name="Brisk", active=True, enabled=False)
    assert got is not None and (got.name, got.active, got.enabled) == ("Brisk", True, False)
    picked = {r.name: r.active for r in await repo.list(OWNER)}
    assert picked == {"A": False, "Brisk": True, "T": True, "C": True}
    # A picked row that changes motion leaves its pick behind.
    moved = await repo.update(OWNER, t.id, motion="trick")
    assert moved is not None and (moved.motion, moved.active) == ("trick", False)
    # Nothing given changes nothing.
    same = await repo.update(OWNER, c.id)
    assert same is not None and (same.active, same.motion, same.enabled) == (True, "walk", True)
    assert await repo.update(OWNER, "00000000-0000-0000-0000-000000000000", enabled=False) is None
    with pytest.raises(ValueError, match="motion"):
        await repo.update(OWNER, a.id, motion="dance")
    async with scoped_session(maker, OWNER) as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE app.growbot_policies SET active = true, motion = 'walk'"
                    " WHERE source_spec = 'growbot_camera_walk'"
                )
            )


async def test_two_picks_at_once_both_land_cleanly(maker: async_sessionmaker) -> None:
    """The family lock queues the second pick, which then unpicks the first."""
    repo = SqlPolicyRepo(maker)
    rows = [await repo.add(OWNER, n, POLICY, 10, n, "growbot_camera_walk") for n in "ABCD"]
    await asyncio.gather(*(repo.update(OWNER, r.id, active=True) for r in rows))
    assert sum(r.active for r in await repo.list(OWNER)) == 1


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
    # Renaming that row by id is refused and leaves its name as it was.
    async with scoped_session(maker, SYSTEM_OWNER) as session:
        hollow = (
            await session.execute(text("SELECT id FROM app.growbot_policies WHERE name = 'hollow'"))
        ).scalar_one()
    with pytest.raises(PolicyError):
        await repo.rename(OWNER, str(hollow), "renamed")
    async with scoped_session(maker, SYSTEM_OWNER) as session:
        names = (await session.execute(text("SELECT name FROM app.growbot_policies"))).scalars()
        assert sorted(names) == ["good", "hollow"]


async def test_the_library_is_owner_only(maker: async_sessionmaker) -> None:
    repo = SqlPolicyRepo(maker)
    row = await repo.add(OWNER, "a", POLICY, 1, "", "")
    for ctx in NON_OWNERS:
        assert await repo.list(ctx) == [], ctx
        assert await repo.get_json(ctx, row.id) is None, ctx
        assert not await repo.exists(ctx, row.id), ctx
        assert await repo.rename(ctx, row.id, "x") is None, ctx
        assert not await repo.delete(ctx, row.id), ctx
        assert not await repo.replace_policy(ctx, row.id, {"x": 1}, 1, "repo:x@y"), ctx
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


async def test_a_kept_policy_is_replaced_in_place(maker: async_sessionmaker) -> None:
    repo = SqlPolicyRepo(maker)
    row = await repo.add(OWNER, "Look v1", POLICY, 10, "repo:look-v1@" + "a" * 40, "growbot_look")
    await repo.rename(OWNER, row.id, "Steady")
    newer = {**POLICY, "mean": [0.5, 0.5]}
    tag = "repo:look-v1@" + "b" * 40
    assert await repo.replace_policy(OWNER, row.id, newer, 20, tag)
    [got] = await repo.list(OWNER)
    assert (got.id, got.name, got.bytes, got.source_job) == (row.id, "Steady", 20, tag)
    assert await repo.get_json(OWNER, row.id) == newer
    assert not await repo.replace_policy(
        OWNER, "00000000-0000-0000-0000-000000000000", newer, 1, tag
    )


async def test_a_masters_teachers_come_back_from_every_read(maker: async_sessionmaker) -> None:
    repo = SqlPolicyRepo(maker)
    master = {
        **POLICY,
        "training": {
            "master": {
                "teachers": {
                    "walk": "policies/lift-fall-3/policy_lift_fall.json",
                    "look": "policies/look-v9/policy_look.json",
                }
            }
        },
    }
    want = (("look", "look-v9"), ("walk", "lift-fall-3"))
    row = await repo.add(OWNER, "Master v2", master, 10, "job1", "growbot_master")
    assert row.teachers == want
    assert [r.teachers for r in await repo.list(OWNER)] == [want]
    renamed = await repo.update(OWNER, row.id, name="Steady")
    assert renamed is not None and renamed.teachers == want
    plain = await repo.add(OWNER, "Look", POLICY, 10, "job2", "growbot_look")
    assert plain.teachers == ()
