"""The view-scope helpers and the audit write are pinned to the caller (SECURITY S5, 0242).

Two halves. The attack: a member session handing `viewer_may_see` / `visible_subjects`
someone else's subject gets false / nothing, and a member can't write a who-saw-whom row
about a subject it may not see. The flows that must not move: the member dashboard's
roster, positions and timeline over the real app, the live map's visibility + audit under
the real repos, the MQTT/live `may_view`, push routing (owner context), and the owner
asking about anyone.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.api.live import deliver_fix, viewer_context
from jbrain.auth import keys
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, device_context, scoped_session
from jbrain.locations import SqlLocationRepo
from jbrain.locations.live import LiveFix
from jbrain.locations.viewscope import SqlViewScopeRepo
from jbrain.main import create_app
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

NARROWED_OWNER = SessionContext(
    principal_id=str(uuid.uuid4()),
    principal_kind="owner",
    domain_scopes=("location",),
    owner_scoped=True,
)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _device(maker: async_sessionmaker, name: str, key: str = "") -> tuple[str, str]:
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, :n, 'device')"),
            {"s": sid, "n": name},
        )
        await session.execute(
            text(
                "INSERT INTO app.principals (id, kind, subject_id, key_hash, label)"
                " VALUES (:p, 'device_key', :s, :k, :n)"
            ),
            {"p": pid, "s": sid, "k": keys.hash_key(key or uuid.uuid4().hex), "n": name},
        )
    return pid, sid


async def _group(maker: async_sessionmaker, members: list[str]) -> None:
    async with scoped_session(maker, OWNER) as session:
        gid = (
            await session.execute(
                text("INSERT INTO app.family_group (name) VALUES ('fam') RETURNING id")
            )
        ).scalar()
        for sid in members:
            await session.execute(
                text("INSERT INTO app.view_scope (group_id, member_subject_id) VALUES (:g, :s)"),
                {"g": str(gid), "s": sid},
            )


async def _fix(maker: async_sessionmaker, pid: str, sid: str, when: datetime) -> None:
    async with scoped_session(maker, device_context(pid, sid)) as session:
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, captured_at, latitude, longitude)"
                " VALUES (:s, :p, :t, 40.0, -74.0)"
            ),
            {"s": sid, "p": pid, "t": when},
        )


async def _scalar(maker: async_sessionmaker, ctx: SessionContext, sql: str, **args: str) -> Any:
    async with scoped_session(maker, ctx) as session:
        return (await session.execute(text(sql), args)).scalar()


async def _ids(maker: async_sessionmaker, ctx: SessionContext, viewer: str) -> set[str]:
    async with scoped_session(maker, ctx) as session:
        rows = await session.execute(
            text("SELECT subject_id::text FROM app.visible_subjects(:v)"), {"v": viewer}
        )
        return set(rows.scalars().all())


async def _audits(maker: async_sessionmaker, viewer: str, target: str, path: str) -> int:
    return await _scalar(
        maker,
        OWNER,
        "SELECT count(*) FROM app.view_audit"
        " WHERE viewer_subject_id = cast(:v AS uuid) AND target_subject_id = cast(:t AS uuid)"
        " AND path = :p",
        v=viewer,
        t=target,
        p=path,
    )


async def test_a_member_cannot_ask_on_another_viewers_behalf(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    _, sid_b = await _device(maker, "Bob")
    _, sid_c = await _device(maker, "Carol")
    _, sid_d = await _device(maker, "Dan")
    await _group(maker, [sid_a, sid_b])
    await _group(maker, [sid_c, sid_d])
    alice = device_context(pid_a, sid_a)
    may = "SELECT app.viewer_may_see(:v, :t)"

    # As herself: unchanged.
    assert await _scalar(maker, alice, may, v=sid_a, t=sid_b) is True
    assert await _ids(maker, alice, sid_a) == {sid_a, sid_b}
    # As Carol, whose family she is not in: no answer and no roster (was true / names).
    assert await _scalar(maker, alice, may, v=sid_c, t=sid_d) is False
    assert await _ids(maker, alice, sid_c) == set()
    # A session with no subject at all (the old empty ctx) gets nothing either.
    assert await _scalar(maker, SessionContext(), may, v=sid_a, t=sid_b) is False
    assert await _ids(maker, SessionContext(), sid_a) == set()


async def test_owner_sessions_still_ask_about_anyone(maker: async_sessionmaker) -> None:
    _, sid_a = await _device(maker, "Alice")
    _, sid_b = await _device(maker, "Bob")
    _, sid_c = await _device(maker, "Carol")
    await _group(maker, [sid_a, sid_b])
    may = "SELECT app.viewer_may_see(:v, :t)"

    for ctx in (OWNER, SYSTEM_CTX, NARROWED_OWNER):
        assert await _scalar(maker, ctx, may, v=sid_a, t=sid_b) is True
        assert await _scalar(maker, ctx, may, v=sid_a, t=sid_c) is False
        assert await _ids(maker, ctx, sid_b) == {sid_a, sid_b}


async def test_may_view_answers_as_the_viewer(maker: async_sessionmaker) -> None:
    """The live socket and the MQTT ACL ask through `SqlViewScopeRepo`, which now runs as
    the viewer; it must still say yes for the family and no otherwise."""
    _, sid_a = await _device(maker, "Alice")
    _, sid_b = await _device(maker, "Bob")
    _, sid_c = await _device(maker, "Carol")
    await _group(maker, [sid_a, sid_b])
    repo = SqlViewScopeRepo(maker)

    assert await repo.may_view(sid_a, sid_b) is True
    assert await repo.may_view(sid_b, sid_a) is True
    assert await repo.may_view(sid_a, sid_c) is False


async def test_audit_rows_only_about_self_or_a_visible_subject(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    _, sid_b = await _device(maker, "Bob")
    _, sid_c = await _device(maker, "Carol")
    pid_s, sid_s = await _device(maker, "Solo")
    await _group(maker, [sid_a, sid_b])
    repo = SqlLocationRepo(maker)

    async def view(ctx: SessionContext, pid: str, viewer: str, target: str) -> None:
        await repo.record_view(
            ctx,
            viewer_principal_id=pid,
            viewer_subject_id=viewer,
            target_subject_id=target,
            path="history",
        )

    alice = device_context(pid_a, sid_a)
    await view(alice, pid_a, sid_a, sid_b)  # a family member
    await view(alice, pid_a, sid_a, sid_a)  # herself
    # A member in no family group still audits its own track (the live socket does).
    await view(device_context(pid_s, sid_s), pid_s, sid_s, sid_s)
    assert await _audits(maker, sid_a, sid_b, "history") == 1
    assert await _audits(maker, sid_s, sid_s, "history") == 1

    # The forgery: "Alice viewed Carol", Carol being no one Alice may see.
    with pytest.raises(ProgrammingError):
        await view(alice, pid_a, sid_a, sid_c)
    # The owner writes any row, as before.
    await view(OWNER, str(uuid.uuid4()), "", sid_c)


async def test_live_map_delivers_and_audits_the_family_under_real_rls(
    maker: async_sessionmaker,
) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    _, sid_b = await _device(maker, "Bob")
    _, sid_c = await _device(maker, "Carol")
    await _group(maker, [sid_a, sid_b])
    member = PrincipalInfo(id=pid_a, kind="device_key", label="Alice", subject_id=sid_a)
    sent: list[dict] = []

    async def send(payload: dict) -> None:
        sent.append(payload)

    audited: set[str] = set()
    visible: dict[str, bool] = {}
    for sid in (sid_a, sid_b, sid_c):
        fix = LiveFix(
            subject_id=sid,
            latitude=40.0,
            longitude=-74.0,
            accuracy_m=None,
            battery_pct=None,
            velocity_mps=None,
            captured_at=datetime.now(UTC),
        )
        await deliver_fix(
            send,
            SqlLocationRepo(maker),
            SqlViewScopeRepo(maker),
            viewer_context(member),
            member,
            fix,
            audited,
            visible,
        )

    assert [p["subject_id"] for p in sent] == [sid_a, sid_b]
    assert await _audits(maker, sid_a, sid_b, "live") == 1
    assert await _audits(maker, sid_a, sid_a, "live") == 1
    assert await _audits(maker, sid_a, sid_c, "live") == 0


async def test_member_dashboard_routes_over_the_real_app(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker,
) -> None:
    key = f"jb1-{uuid.uuid4().hex}"
    pid_a, sid_a = await _device(maker, "Alice", key)
    pid_b, sid_b = await _device(maker, "Bob")
    pid_c, sid_c = await _device(maker, "Carol")
    await _group(maker, [sid_a, sid_b])
    now = datetime.now(UTC)
    for pid, sid in ((pid_a, sid_a), (pid_b, sid_b), (pid_c, sid_c)):
        await _fix(maker, pid, sid, now - timedelta(minutes=5))

    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        assert client.post("/api/session/mint", json={"device_key": key}).status_code == 204
        roster = client.get("/api/member/roster")
        assert roster.status_code == 200
        assert {r["subject_id"] for r in roster.json()} == {sid_a, sid_b}
        bob = client.get("/api/member/positions", params={"subject_id": sid_b})
        assert bob.status_code == 200 and len(bob.json()) == 1
        carol = client.get("/api/member/positions", params={"subject_id": sid_c})
        assert carol.status_code == 200 and carol.json() == []
        assert client.get("/api/member/timeline").status_code == 200

    assert await _audits(maker, sid_a, sid_b, "roster") == 1
    assert await _audits(maker, sid_a, sid_b, "history") == 1
    assert await _audits(maker, sid_a, sid_c, "history") == 0
