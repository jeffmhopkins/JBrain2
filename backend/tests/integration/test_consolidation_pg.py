"""Retroactive predicate consolidation against real Postgres: a drift spelling
moves onto its canonical address in place, a collision with an existing
canonical fact is left alone (never chain-merged), and a clean corpus is a
no-op."""

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

from jbrain.analysis.consolidation import consolidate_predicates
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


async def _seed(
    maker: async_sessionmaker[AsyncSession], *, kind: str = "Person"
) -> tuple[uuid.UUID, uuid.UUID]:
    note_id, entity_id = uuid.uuid4(), uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, created_at)"
                " VALUES (:i, :c, 'general', 'seed', :t)"
            ),
            {"i": str(note_id), "c": str(note_id)[:12], "t": NOTE_TIME},
        )
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, status, domain_code)"
                " VALUES (:i, :k, 'E', 'provisional', 'general')"
            ),
            {"i": str(entity_id), "k": kind},
        )
    return note_id, entity_id


async def _seed_fact(
    maker: async_sessionmaker[AsyncSession],
    *,
    entity_id: uuid.UUID,
    note_id: uuid.UUID,
    predicate: str,
) -> None:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, qualifier, kind, statement,"
                " assertion, reported_at, temporal_precision, status, note_id, extractor,"
                " prompt_version, domain_code)"
                " VALUES (:id, :eid, :pred, '', 'attribute', :stmt, 'asserted', :ts, 'unknown',"
                " 'active', :nid, 'test', 'test-v1', 'general')"
            ),
            {
                "id": str(uuid.uuid4()),
                "eid": str(entity_id),
                "pred": predicate,
                "stmt": f"{predicate} fact",
                "ts": NOTE_TIME,
                "nid": str(note_id),
            },
        )


async def _predicates(maker: async_sessionmaker[AsyncSession], entity_id: uuid.UUID) -> set[str]:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        rows = (
            await s.execute(
                text("SELECT predicate FROM app.facts WHERE entity_id = :id"),
                {"id": str(entity_id)},
            )
        ).all()
    return {r.predicate for r in rows}


async def test_drift_predicate_moves_to_canonical(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note, entity = await _seed(maker)
    await _seed_fact(maker, entity_id=entity, note_id=note, predicate="legalName")
    async with scoped_session(maker, SYSTEM_CTX) as s:
        counts = await consolidate_predicates(s)
    assert counts == {"renamed": 1, "collisions": 0}
    assert await _predicates(maker, entity) == {"name.legal"}


async def test_collision_with_existing_canonical_is_left_alone(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note, entity = await _seed(maker)
    await _seed_fact(maker, entity_id=entity, note_id=note, predicate="name.legal")
    await _seed_fact(maker, entity_id=entity, note_id=note, predicate="legalName")
    async with scoped_session(maker, SYSTEM_CTX) as s:
        counts = await consolidate_predicates(s)
    # The drift fact cannot merge onto the occupied key: both spellings remain.
    assert counts == {"renamed": 0, "collisions": 1}
    assert await _predicates(maker, entity) == {"name.legal", "legalName"}


async def test_sweep_is_idempotent(maker: async_sessionmaker[AsyncSession]) -> None:
    note, entity = await _seed(maker)
    await _seed_fact(maker, entity_id=entity, note_id=note, predicate="alsoKnownAs")
    async with scoped_session(maker, SYSTEM_CTX) as s:
        first = await consolidate_predicates(s)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        second = await consolidate_predicates(s)
    assert first == {"renamed": 1, "collisions": 0}
    assert second == {"renamed": 0, "collisions": 0}
    assert await _predicates(maker, entity) == {"name.nickname"}


async def test_distinct_entities_are_independent(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note, e1 = await _seed(maker)
    _, e2 = await _seed(maker)
    await _seed_fact(maker, entity_id=e1, note_id=note, predicate="legalName")  # no canonical yet
    await _seed_fact(maker, entity_id=e2, note_id=note, predicate="name.legal")
    await _seed_fact(maker, entity_id=e2, note_id=note, predicate="legalName")  # blocked by e2's
    async with scoped_session(maker, SYSTEM_CTX) as s:
        counts = await consolidate_predicates(s)
    assert counts == {"renamed": 1, "collisions": 1}
    assert await _predicates(maker, e1) == {"name.legal"}
    assert await _predicates(maker, e2) == {"name.legal", "legalName"}
