"""Canonical-name projection and provisional -> confirmed promotion against
real Postgres: a frozen first-mention name (the "Sammy" bug) reprojects from
the entity's current name.* facts, the owner "Me" override is preserved, and a
second note's corroboration confirms a provisional entity."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.analysis.canonical import reproject_canonical_name
from jbrain.db.session import scoped_session
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_rls import APP_PASSWORD, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

NOTE_TIME = datetime(2026, 6, 11, 16, 0, tzinfo=UTC)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean(database_url: str) -> AsyncIterator[None]:  # noqa: F811
    admin_url = database_url.replace(f"jbrain_app:{APP_PASSWORD}", "test:test")
    engine = create_async_engine(admin_url, poolclass=NullPool)
    async with async_sessionmaker(engine)() as s:
        await s.execute(
            text(
                "TRUNCATE app.facts, app.entities, app.entity_mentions, app.entity_aliases,"
                " app.temporal_tokens, app.review_items, app.note_analysis,"
                " app.chunks, app.notes, app.subjects CASCADE"
            )
        )
        await s.commit()
    await engine.dispose()
    yield


# --- seeding ----------------------------------------------------------------


async def seed_note(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    note_id = uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, created_at)"
                " VALUES (:i, :c, 'general', 'seed', :t)"
            ),
            {"i": str(note_id), "c": str(note_id)[:12], "t": NOTE_TIME},
        )
    return note_id


async def seed_entity(
    maker: async_sessionmaker[AsyncSession],
    *,
    name: str,
    kind: str = "Person",
    status: str = "provisional",
    with_subject: bool = False,
) -> uuid.UUID:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        subject_id = None
        if with_subject:
            subject_id = uuid.uuid4()
            await s.execute(
                text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:i, :n, 'person')"),
                {"i": str(subject_id), "n": name},
            )
        entity_id = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, status, subject_id,"
                " domain_code) VALUES (:i, :k, :n, :st, :sub, 'general')"
            ),
            {
                "i": str(entity_id),
                "k": kind,
                "n": name,
                "st": status,
                "sub": str(subject_id) if subject_id else None,
            },
        )
    return entity_id


async def seed_fact(
    maker: async_sessionmaker[AsyncSession],
    *,
    entity_id: uuid.UUID,
    note_id: uuid.UUID,
    predicate: str,
    value_json: dict | None,
) -> None:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, qualifier, kind, statement,"
                " value_json, assertion, reported_at, temporal_precision, status, note_id,"
                " extractor, prompt_version, domain_code)"
                " VALUES (:id, :eid, :pred, '', 'attribute', :stmt, CAST(:vj AS jsonb),"
                " 'asserted', :ts, 'unknown', 'active', :nid, 'test', 'test-v1', 'general')"
            ),
            {
                "id": str(uuid.uuid4()),
                "eid": str(entity_id),
                "pred": predicate,
                "stmt": f"{predicate} fact",
                "vj": json.dumps(value_json) if value_json is not None else None,
                "ts": NOTE_TIME,
                "nid": str(note_id),
            },
        )


async def _canonical(maker: async_sessionmaker[AsyncSession], entity_id: uuid.UUID) -> str:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (
            await s.execute(
                text("SELECT canonical_name FROM app.entities WHERE id = :id"),
                {"id": str(entity_id)},
            )
        ).scalar_one()


# --- projection -------------------------------------------------------------


async def test_reproject_replaces_frozen_nickname_with_legal_name(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note = await seed_note(maker)
    entity = await seed_entity(maker, name="Sammy")  # frozen first surface form
    await seed_fact(
        maker,
        entity_id=entity,
        note_id=note,
        predicate="name.legal",
        value_json={"value": "Celine Kitina Hopkins"},
    )
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await reproject_canonical_name(s, entity) == "Celine Kitina Hopkins"
    assert await _canonical(maker, entity) == "Celine Kitina Hopkins"


async def test_reproject_prefers_preferred_name(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note = await seed_note(maker)
    entity = await seed_entity(maker, name="Sammy")
    await seed_fact(maker, entity_id=entity, note_id=note, predicate="name.legal",
                    value_json={"value": "Celine Kitina Hopkins"})  # fmt: skip
    await seed_fact(maker, entity_id=entity, note_id=note, predicate="name.preferred",
                    value_json={"value": "Sam"})  # fmt: skip
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await reproject_canonical_name(s, entity) == "Sam"


async def test_reproject_composes_given_and_family(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note = await seed_note(maker)
    entity = await seed_entity(maker, name="Celine Hopkins")
    await seed_fact(maker, entity_id=entity, note_id=note, predicate="name.given",
                    value_json={"value": "Celine"})  # fmt: skip
    await seed_fact(maker, entity_id=entity, note_id=note, predicate="name.family",
                    value_json={"value": "Kitina"})  # fmt: skip
    async with scoped_session(maker, SYSTEM_CTX) as s:
        # No legal/preferred -> compose; differs from the seeded "Celine Hopkins".
        assert await reproject_canonical_name(s, entity) == "Celine Kitina"


async def test_owner_me_keeps_its_override(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note = await seed_note(maker)
    me = await seed_entity(maker, name="Me", status="confirmed", with_subject=True)
    await seed_fact(maker, entity_id=me, note_id=note, predicate="name.legal",
                    value_json={"value": "Jeffrey Mark Hopkins"})  # fmt: skip
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await reproject_canonical_name(s, me) is None
    assert await _canonical(maker, me) == "Me"


async def test_unknown_kind_is_left_alone(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note = await seed_note(maker)
    entity = await seed_entity(maker, name="aspirin", kind="Drug")
    await seed_fact(maker, entity_id=entity, note_id=note, predicate="name.legal",
                    value_json={"value": "acetylsalicylic acid"})  # fmt: skip
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await reproject_canonical_name(s, entity) is None
    assert await _canonical(maker, entity) == "aspirin"


async def test_no_usable_value_keeps_existing_name(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    # The real-world Sammy case: the legal name lived only in the statement, with
    # a null value_json -> nothing to project, so the name is left untouched.
    note = await seed_note(maker)
    entity = await seed_entity(maker, name="Sammy")
    await seed_fact(maker, entity_id=entity, note_id=note, predicate="name.legal", value_json=None)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await reproject_canonical_name(s, entity) is None
    assert await _canonical(maker, entity) == "Sammy"


async def test_animal_reprojects_via_species_signal(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A pet first mentioned by a reference ("the rat", kind=species) reprojects
    to its declared name: its species kind matches no registry type, but the
    decomposed name fact's species key identifies it as an Animal."""
    note = await seed_note(maker)
    entity = await seed_entity(maker, name="the rat", kind="rat")
    await seed_fact(
        maker,
        entity_id=entity,
        note_id=note,
        predicate="name",
        value_json={"name": "Ricky", "species": "rat"},
    )
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await reproject_canonical_name(s, entity) == "Ricky"
    assert await _canonical(maker, entity) == "Ricky"
