"""Member dashboard reads against real Postgres (JBrain360 M4b, 0070).

Proves the member surface is firewalled by RLS, not by the application: under a
member's `device_context`, `member_roster` resolves only the viewer's own subject
plus its family group (via the `visible_subjects` SECURITY DEFINER helper), and
`fixes` returns only positions for subjects the member may see. A non-member is
neither listed nor readable.
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
from jbrain.locations import SqlLocationRepo
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


async def _fix(maker: async_sessionmaker, pid: str, sid: str, when: datetime) -> None:
    async with scoped_session(maker, device_context(pid, sid)) as session:
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, captured_at, latitude, longitude, battery_pct)"
                " VALUES (:s, :p, :t, 40.0, -74.0, 55)"
            ),
            {"s": sid, "p": pid, "t": when},
        )


async def _group(maker: async_sessionmaker, members: list[str]) -> None:
    async with scoped_session(maker, OWNER) as session:
        gid = (
            await session.execute(
                text("INSERT INTO app.family_group (name) VALUES ('family') RETURNING id")
            )
        ).scalar()
        for sid in members:
            await session.execute(
                text("INSERT INTO app.view_scope (group_id, member_subject_id) VALUES (:g, :s)"),
                {"g": str(gid), "s": sid},
            )


async def _place(maker: async_sessionmaker, name: str, *, shared: bool) -> str:
    """A Place entity + a subject-less circular geofence mirror; shared via place_share
    when asked. Returns the place entity id."""
    async with scoped_session(maker, OWNER) as session:
        eid = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), 'Place', :n, 'location') RETURNING id::text"
                ),
                {"n": name},
            )
        ).scalar()
        await session.execute(
            text(
                "INSERT INTO app.place_geofence"
                " (place_entity_id, domain_code, name, center, radius_m)"
                " VALUES (cast(:e AS uuid), 'location', :n,"
                " ST_SetSRID(ST_MakePoint(-74.0, 40.0), 4326)::geography, 150)"
            ),
            {"e": eid, "n": name},
        )
        if shared:
            await session.execute(
                text("INSERT INTO app.place_share (place_entity_id) VALUES (cast(:e AS uuid))"),
                {"e": eid},
            )
    return str(eid)


async def _crossing(
    maker: async_sessionmaker, *, pid: str, sid: str, eid: str, transition: str, when: datetime
) -> None:
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.events (id, type, payload, domain_code, principal_id, occurred_at)"
                " VALUES (gen_random_uuid(), 'location.geofence_transition',"
                "   cast(:pl AS jsonb), 'location', :pr, :ts)"
            ),
            {
                "pl": f'{{"subject_id": "{sid}", "place_entity_id": "{eid}",'
                f' "transition": "{transition}"}}',
                "pr": pid,
                "ts": when,
            },
        )


WHEN = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
WINDOW = {"since": datetime(2026, 6, 1, tzinfo=UTC), "until": datetime(2026, 7, 1, tzinfo=UTC)}


async def test_member_roster_lists_self_plus_group_with_labels(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    pid_b, sid_b = await _device(maker, "Bob")
    _, sid_c = await _device(maker, "Carol")  # unrelated, not in the group
    await _fix(maker, pid_a, sid_a, WHEN)
    # Bob has no fix yet — must still appear in the picker.
    await _group(maker, [sid_a, sid_b])

    repo = SqlLocationRepo(maker)
    roster = await repo.member_roster(device_context(pid_a, sid_a), viewer_subject_id=sid_a)
    by_id = {m.subject_id: m for m in roster}

    assert set(by_id) == {sid_a, sid_b}  # Carol is invisible
    assert by_id[sid_a].label == "Alice" and by_id[sid_a].last_seen == WHEN
    assert by_id[sid_b].label == "Bob" and by_id[sid_b].last_seen is None
    assert sid_c not in by_id


async def test_member_positions_scoped_to_visible_subjects(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    pid_b, sid_b = await _device(maker, "Bob")
    pid_c, sid_c = await _device(maker, "Carol")
    await _fix(maker, pid_b, sid_b, WHEN)
    await _fix(maker, pid_c, sid_c, WHEN)
    await _group(maker, [sid_a, sid_b])

    repo = SqlLocationRepo(maker)
    ctx = device_context(pid_a, sid_a)
    window = {"since": datetime(2026, 6, 1, tzinfo=UTC), "until": datetime(2026, 7, 1, tzinfo=UTC)}

    # Alice may read group-member Bob's trail...
    bob = await repo.fixes(ctx, subject_id=sid_b, limit=100, **window)
    assert len(bob) == 1 and bob[0].battery_pct == 55
    # ...but Carol (no shared group) returns nothing — RLS, not an app check.
    carol = await repo.fixes(ctx, subject_id=sid_c, limit=100, **window)
    assert carol == []


async def test_member_roster_before_any_group_is_self_only(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    _, sid_b = await _device(maker, "Bob")
    await _fix(maker, pid_a, sid_a, WHEN)

    repo = SqlLocationRepo(maker)
    roster = await repo.member_roster(device_context(pid_a, sid_a), viewer_subject_id=sid_a)
    assert {m.subject_id for m in roster} == {sid_a}
    assert sid_b not in {m.subject_id for m in roster}


async def test_member_places_shows_only_shared_fences(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    await _place(maker, "Home", shared=True)
    await _place(maker, "Therapist", shared=False)

    repo = SqlLocationRepo(maker)
    places = await repo.member_places(device_context(pid_a, sid_a))
    assert [p.name for p in places] == ["Home"]  # the owner-private fence never appears


async def test_member_timeline_only_shared_places_for_visible_subjects(
    maker: async_sessionmaker,
) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    pid_b, sid_b = await _device(maker, "Bob")
    pid_c, sid_c = await _device(maker, "Carol")  # not in the group
    await _group(maker, [sid_a, sid_b])
    home = await _place(maker, "Home", shared=True)
    clinic = await _place(maker, "Therapist", shared=False)
    await _crossing(maker, pid=pid_a, sid=sid_a, eid=home, transition="enter", when=WHEN)
    await _crossing(maker, pid=pid_b, sid=sid_b, eid=home, transition="enter", when=WHEN)
    await _crossing(maker, pid=pid_c, sid=sid_c, eid=home, transition="enter", when=WHEN)
    await _crossing(maker, pid=pid_a, sid=sid_a, eid=clinic, transition="enter", when=WHEN)

    repo = SqlLocationRepo(maker)
    rows = await repo.member_timeline(
        device_context(pid_a, sid_a), viewer_subject_id=sid_a, limit=100, **WINDOW
    )
    seen = {(r.subject_id, r.place_name) for r in rows}
    # Alice + group-member Bob at the SHARED Home only.
    assert seen == {(sid_a, "Home"), (sid_b, "Home")}
    # Carol (not visible) is absent; the un-shared Therapist crossing is absent.
    assert not any(r.subject_id == sid_c for r in rows)
    assert not any(r.place_name == "Therapist" for r in rows)


async def test_place_share_is_owner_write_member_read(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    eid = await _place(maker, "Home", shared=True)
    dev = device_context(pid_a, sid_a)

    # Counts are scoped to THIS place's id — the module DB persists across tests, so a
    # global count would also see other tests' shared places.
    one = text("SELECT count(*) FROM app.place_share WHERE place_entity_id = cast(:e AS uuid)")

    # A member may READ the shared set (its overlay/timeline join needs it)...
    async with scoped_session(maker, dev) as session:
        assert (await session.execute(one, {"e": eid})).scalar() == 1

    # ...but may NOT insert (WITH CHECK is full-owner — a forged share row is rejected).
    other = await _place(maker, "Clinic", shared=False)
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, dev) as session:
            await session.execute(
                text("INSERT INTO app.place_share (place_entity_id) VALUES (cast(:e AS uuid))"),
                {"e": other},
            )

    # ...and a member's DELETE is a silent RLS no-op (USING hides the row), not a way
    # to un-share: the row survives.
    async with scoped_session(maker, dev) as session:
        await session.execute(
            text("DELETE FROM app.place_share WHERE place_entity_id = cast(:e AS uuid)"),
            {"e": eid},
        )
    async with scoped_session(maker, OWNER) as session:
        assert (await session.execute(one, {"e": eid})).scalar() == 1

    # set_place_shared under the owner toggles the row off.
    repo = SqlLocationRepo(maker)
    await repo.set_place_shared(OWNER, place_entity_id=eid, shared=False)
    async with scoped_session(maker, OWNER) as session:
        assert (await session.execute(one, {"e": eid})).scalar() == 0
