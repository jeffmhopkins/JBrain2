"""A device's `location` scope grants only its explicit arms (SECURITY round 2, 0245).

`device_context` carries `domain_scopes=('location',)`. Before 0245 `has_domain_scope`
honoured that claim for any principal kind, so a member phone passed every
location-domain policy guarded by it alone — notes, chunks, entities, facts, events,
review items. These prove the scope arm is owner-only now: a device reads and writes none
of that, still runs its own fixed queries (the flows have their own suites: member
reads, geofence detect, view audit, FCM, OwnTracks/MQTT ingest), and reads only the
geofence events its member timeline needs.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, device_context, panel_context, scoped_session
from jbrain.locations import SqlLocationRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

WHEN = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
_KNOWLEDGE = ("notes", "chunks", "entities", "entity_mentions", "facts", "review_items")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _device(maker: async_sessionmaker, name: str) -> tuple[str, str]:
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, :n, 'device')"),
            {"s": sid, "n": name},
        )
        await s.execute(
            text(
                "INSERT INTO app.principals (id, kind, subject_id, key_hash)"
                " VALUES (:p, 'device_key', :s, :k)"
            ),
            {"p": pid, "s": sid, "k": uuid.uuid4().hex},
        )
    return pid, sid


async def _seed_location_knowledge(maker: async_sessionmaker) -> tuple[str, str]:
    """One location-domain row in each owner-knowledge table a device must not reach."""
    note, chunk, ent = (str(uuid.uuid4()) for _ in range(3))
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, 'location', 'Dad was at the clinic at 3')"
            ),
            {"id": note, "cid": f"dev-{note[:13]}"},
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:id, :n, 'location', 'paragraph', 0, 'Dad was at the clinic')"
            ),
            {"id": chunk, "n": note},
        )
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:id, 'Place', 'Clinic', 'location')"
            ),
            {"id": ent},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_mentions (id, entity_id, chunk_id, note_id,"
                " surface_text, char_start, char_end, link_method, domain_code)"
                " VALUES (gen_random_uuid(), :e, :c, :n, 'clinic', 15, 21, 'exact_alias',"
                " 'location')"
            ),
            {"e": ent, "c": chunk, "n": note},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, assertion,"
                " reported_at, note_id, extractor, prompt_version, domain_code)"
                " VALUES (gen_random_uuid(), :e, 'visitedBy', 'relationship', 'Dad visited',"
                " 'asserted', now(), :n, 'fake-model', 'v1', 'location')"
            ),
            {"e": ent, "n": note},
        )
        await s.execute(
            text(
                "INSERT INTO app.review_items (id, kind, domain_code)"
                " VALUES (gen_random_uuid(), 'merge_proposal', 'location')"
            )
        )
    return note, ent


async def _event(maker: async_sessionmaker, *, type_: str, subject: str, principal: str) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.events (id, type, payload, domain_code, principal_id)"
                " VALUES (gen_random_uuid(), :t, cast(:pl AS jsonb), 'location', :p)"
            ),
            {"t": type_, "pl": f'{{"subject_id": "{subject}"}}', "p": principal},
        )


async def _count(maker: async_sessionmaker, ctx: SessionContext, table: str) -> int:
    async with scoped_session(maker, ctx) as s:
        return (await s.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar_one()


async def test_a_device_reads_no_location_knowledge(maker: async_sessionmaker) -> None:
    pid, sid = await _device(maker, "phone")
    await _seed_location_knowledge(maker)
    for table in _KNOWLEDGE:
        assert await _count(maker, OWNER, table) >= 1, table
        assert await _count(maker, device_context(pid, sid), table) == 0, table


async def test_a_device_writes_no_location_knowledge(maker: async_sessionmaker) -> None:
    pid, sid = await _device(maker, "phone")
    note, ent = await _seed_location_knowledge(maker)
    dev = device_context(pid, sid)
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, dev) as s:
            await s.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (gen_random_uuid(), 'dev-smuggle', 'location', 'planted')"
                )
            )
    async with scoped_session(maker, dev) as s:
        upd = await s.execute(text("UPDATE app.notes SET body = 'rewritten'"))
        dele = await s.execute(text("DELETE FROM app.entities"))
    assert (upd.rowcount, dele.rowcount) == (0, 0)  # type: ignore[attr-defined]
    async with scoped_session(maker, OWNER) as s:
        body = (
            await s.execute(text("SELECT body FROM app.notes WHERE id = :i"), {"i": note})
        ).scalar_one()
        kept = (
            await s.execute(text("SELECT count(*) FROM app.entities WHERE id = :i"), {"i": ent})
        ).scalar_one()
    assert (body, kept) == ("Dad was at the clinic at 3", 1)


async def test_a_device_reads_only_geofence_events_it_may_see(maker: async_sessionmaker) -> None:
    pid, sid = await _device(maker, "me")
    _, kin = await _device(maker, "kin")
    _, stranger = await _device(maker, "stranger")
    async with scoped_session(maker, OWNER) as s:
        gid = (
            await s.execute(text("INSERT INTO app.family_group (name) VALUES ('f') RETURNING id"))
        ).scalar()
        for member in (sid, kin):
            await s.execute(
                text("INSERT INTO app.view_scope (group_id, member_subject_id) VALUES (:g, :s)"),
                {"g": str(gid), "s": member},
            )
    for subject in (sid, kin, stranger):
        await _event(maker, type_="location.geofence_transition", subject=subject, principal=pid)
    # A location-domain event of another type, about the device's own subject.
    await _event(maker, type_="note.created", subject=sid, principal=pid)

    async with scoped_session(maker, device_context(pid, sid)) as s:
        seen = set(
            (await s.execute(text("SELECT payload->>'subject_id' FROM app.events"))).scalars()
        )
        kinds = set((await s.execute(text("SELECT type FROM app.events"))).scalars())
    assert seen == {sid, kin}
    assert kinds == {"location.geofence_transition"}

    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, device_context(pid, sid)) as s:
            await s.execute(
                text(
                    "INSERT INTO app.events (id, type, payload, domain_code, principal_id)"
                    " VALUES (gen_random_uuid(), 'location.geofence_transition',"
                    " cast(:pl AS jsonb), 'location', :p)"
                ),
                {"pl": f'{{"subject_id": "{sid}"}}', "p": pid},
            )


async def test_a_device_still_runs_its_own_location_queries(maker: async_sessionmaker) -> None:
    # The explicit device arms: ingest its own fix and read it back; a panel (a
    # device_key with no location scope) on the same subject earns none of them.
    pid, sid = await _device(maker, "phone")
    repo = SqlLocationRepo(maker)
    async with scoped_session(maker, device_context(pid, sid)) as s:
        await s.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, captured_at, latitude, longitude)"
                " VALUES (:s, :p, :t, 40.0, -74.0)"
            ),
            {"s": sid, "p": pid, "t": WHEN},
        )
    fixes = await repo.fixes(
        device_context(pid, sid),
        subject_id=sid,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 7, 1, tzinfo=UTC),
        limit=10,
    )
    assert len(fixes) == 1
    assert await _count(maker, panel_context(pid, sid), "location_fixes") == 0


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (OWNER, {"general": True, "location": True}),
        (
            SessionContext(principal_kind="owner", domain_scopes=("location",), owner_scoped=True),
            {"general": False, "location": True},
        ),
        (
            device_context(str(uuid.uuid4()), str(uuid.uuid4())),
            {"general": False, "location": False},
        ),
        (
            SessionContext(principal_kind="capability_token", domain_scopes=("general",)),
            {"general": False, "location": False},
        ),
        (
            SessionContext(principal_kind="jmolt_observer", domain_scopes=("jmolt",)),
            {"general": False, "location": False},
        ),
    ],
)
async def test_has_domain_scope_is_owner_only(
    maker: async_sessionmaker, ctx: SessionContext, expected: dict[str, bool]
) -> None:
    async with scoped_session(maker, ctx) as s:
        got = {
            code: (
                await s.execute(text("SELECT app.has_domain_scope(:c)"), {"c": code})
            ).scalar_one()
            for code in expected
        }
    assert got == expected
