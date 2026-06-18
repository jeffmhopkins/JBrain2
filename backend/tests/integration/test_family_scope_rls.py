"""Family-group view-scope firewall against real Postgres (JBrain360 M2a, 0067).

The third visibility path on the location firewall: two subjects in the same family
group may READ each other's fixes (`viewer_may_see`), enforced in Postgres RLS —
while writes stay subject-pinned and the group tables themselves are owner-only.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import device_context, scoped_session
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


async def _device(maker: async_sessionmaker, name: str) -> tuple[str, str]:
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, :n, 'device')"),
            {"s": sid, "n": name},
        )
        await session.execute(
            text(
                "INSERT INTO app.principals (id, kind, subject_id, key_hash)"
                " VALUES (:p, 'device_key', :s, :k)"
            ),
            {"p": pid, "s": sid, "k": uuid.uuid4().hex},
        )
    return pid, sid


async def _fix(maker: async_sessionmaker, ctx, *, sid: str, pid: str, when: datetime) -> None:
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, captured_at, latitude, longitude)"
                " VALUES (:s, :p, :t, 40.0, -74.0)"
            ),
            {"s": sid, "p": pid, "t": when},
        )


async def _group(maker: async_sessionmaker, name: str, members: list[str]) -> str:
    async with scoped_session(maker, OWNER) as session:
        gid = (
            await session.execute(
                text("INSERT INTO app.family_group (name) VALUES (:n) RETURNING id"), {"n": name}
            )
        ).scalar()
        for sid in members:
            await session.execute(
                text("INSERT INTO app.view_scope (group_id, member_subject_id) VALUES (:g, :s)"),
                {"g": str(gid), "s": sid},
            )
    return str(gid)


async def _count(maker: async_sessionmaker, ctx, sql: str, args: dict) -> int:
    async with scoped_session(maker, ctx) as session:
        return (await session.execute(text(sql), args)).scalar() or 0


async def test_group_members_read_each_others_fixes_non_members_cannot(
    maker: async_sessionmaker,
) -> None:
    pid_a, sid_a = await _device(maker, "A")
    pid_b, sid_b = await _device(maker, "B")
    pid_c, sid_c = await _device(maker, "C")
    when = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)
    await _fix(maker, device_context(pid_a, sid_a), sid=sid_a, pid=pid_a, when=when)
    await _fix(maker, device_context(pid_b, sid_b), sid=sid_b, pid=pid_b, when=when)
    await _fix(maker, device_context(pid_c, sid_c), sid=sid_c, pid=pid_c, when=when)

    see_b = "SELECT count(*) FROM app.location_fixes WHERE subject_id = :b"
    see_a = "SELECT count(*) FROM app.location_fixes WHERE subject_id = :a"
    see_c = "SELECT count(*) FROM app.location_fixes WHERE subject_id = :c"

    # Before any group, A sees only its own subject.
    assert await _count(maker, device_context(pid_a, sid_a), see_b, {"b": sid_b}) == 0

    await _group(maker, "family", [sid_a, sid_b])

    # A and B now read each other; C (not a member) reads neither and is unseen by A.
    assert await _count(maker, device_context(pid_a, sid_a), see_b, {"b": sid_b}) == 1
    assert await _count(maker, device_context(pid_b, sid_b), see_a, {"a": sid_a}) == 1
    assert await _count(maker, device_context(pid_c, sid_c), see_b, {"b": sid_b}) == 0
    assert await _count(maker, device_context(pid_a, sid_a), see_c, {"c": sid_c}) == 0


async def test_view_scope_is_read_only_writes_stay_subject_pinned(
    maker: async_sessionmaker,
) -> None:
    pid_a, sid_a = await _device(maker, "A")
    _, sid_b = await _device(maker, "B")
    await _group(maker, "family", [sid_a, sid_b])
    # Even as a group member, A cannot forge a fix for B — WITH CHECK is unchanged.
    with pytest.raises(ProgrammingError):
        await _fix(
            maker,
            device_context(pid_a, sid_a),
            sid=sid_b,
            pid=pid_a,
            when=datetime(2026, 6, 6, 9, 0, tzinfo=UTC),
        )


async def test_family_group_and_view_scope_are_owner_only(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "A")
    gid = await _group(maker, "family", [sid_a])

    # Full owner sees the group + membership.
    assert (
        await _count(
            maker, OWNER, "SELECT count(*) FROM app.family_group WHERE id = :g", {"g": gid}
        )
        == 1
    )
    assert (
        await _count(
            maker, OWNER, "SELECT count(*) FROM app.view_scope WHERE group_id = :g", {"g": gid}
        )
        == 1
    )

    # A device sees zero group / membership rows (the rows feed viewer_may_see via
    # SECURITY DEFINER, never a direct device read)...
    dev = device_context(pid_a, sid_a)
    assert await _count(maker, dev, "SELECT count(*) FROM app.family_group", {}) == 0
    assert await _count(maker, dev, "SELECT count(*) FROM app.view_scope", {}) == 0

    # ...and cannot create a group (WITH CHECK: is_full_owner only).
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, dev) as session:
            await session.execute(text("INSERT INTO app.family_group (name) VALUES ('sneaky')"))
