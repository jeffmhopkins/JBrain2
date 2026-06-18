"""The three Phase-6 hygiene sweeps against real Postgres (docs/HYGIENE_SWEEPS_PLAN.md):
entity_hygiene (orphan delete), reembed_stale (re-embed stale-model rows), tag_consolidate
(canonicalize note tags). Pure-maintenance actions under SYSTEM_CTX; the embedder is faked.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.analysis.hygiene import entity_hygiene_handler
from jbrain.analysis.reembed import reembed_handler
from jbrain.analysis.tagconsolidate import tag_consolidate_handler
from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import APP_PASSWORD, OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class FakeEmbed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.5] * 384 for _ in texts]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _isolate(database_url: str) -> AsyncIterator[None]:  # noqa: F811
    yield
    admin = create_async_engine(
        database_url.replace(f"jbrain_app:{APP_PASSWORD}", "test:test"), poolclass=NullPool
    )
    try:
        async with admin.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE app.entities, app.skills, app.notes, app.note_analysis"
                    " RESTART IDENTITY CASCADE"
                )
            )
    finally:
        await admin.dispose()


async def _entity(maker: async_sessionmaker, *, status: str, subject: bool = False) -> str:
    eid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, status, subject_id,"
                " domain_code) VALUES (:id, 'person', 'X', :s, :subj, 'general')"
            ),
            {"id": eid, "s": status, "subj": eid if subject else None},
        )
    return eid


async def _entity_ids(maker: async_sessionmaker) -> set[str]:
    async with scoped_session(maker, OWNER) as session:
        rows = (await session.execute(text("SELECT id::text FROM app.entities"))).scalars()
        return set(rows)


# --- entity_hygiene -------------------------------------------------------


async def test_entity_hygiene_deletes_a_provisional_orphan(maker: async_sessionmaker) -> None:
    orphan = await _entity(maker, status="provisional")
    await entity_hygiene_handler(maker)({})
    assert orphan not in await _entity_ids(maker)


async def test_entity_hygiene_keeps_confirmed_and_subject_entities(
    maker: async_sessionmaker,
) -> None:
    confirmed = await _entity(maker, status="confirmed")
    subject = await _entity(maker, status="provisional", subject=True)
    await entity_hygiene_handler(maker)({})
    survivors = await _entity_ids(maker)
    assert confirmed in survivors and subject in survivors


async def test_entity_hygiene_keeps_an_entity_with_a_mention(maker: async_sessionmaker) -> None:
    """A provisional entity that a surviving mention references is NOT an orphan."""
    eid = await _entity(maker, status="provisional")
    async with scoped_session(maker, OWNER) as session:
        note = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, 'general', 'b')"
            ),
            {"id": note, "cid": uuid.uuid4().hex},
        )
        chunk = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, idx, kind, body, char_start,"
                " char_end) VALUES (:id, :n, 'general', 0, 'paragraph', 'b', 0, 1)"
            ),
            {"id": chunk, "n": note},
        )
        await session.execute(
            text(
                "INSERT INTO app.entity_mentions (id, entity_id, chunk_id, note_id, surface_text,"
                " char_start, char_end, link_method, domain_code)"
                " VALUES (:id, :e, :c, :n, 'X', 0, 1, 'exact', 'general')"
            ),
            {"id": str(uuid.uuid4()), "e": eid, "c": chunk, "n": note},
        )
    await entity_hygiene_handler(maker)({})
    assert eid in await _entity_ids(maker)


async def test_entity_hygiene_is_idempotent(maker: async_sessionmaker) -> None:
    await _entity(maker, status="provisional")
    await entity_hygiene_handler(maker)({})
    await entity_hygiene_handler(maker)({})  # second run finds nothing, no error
    assert await _entity_ids(maker) == set()


# --- reembed_stale --------------------------------------------------------


async def _skill(maker: async_sessionmaker, *, model: str | None) -> str:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.skills (id, name, version, status, domain_code, body, description,"
                " embedding, embedding_model)"
                " VALUES (:id, :name, 1, 'active', 'general', 'do a thing', 'a skill',"
                "  cast(:emb AS vector), :model)"
            ),
            {
                "id": sid,
                "name": f"s-{sid[:8]}",
                "emb": "[" + ",".join(["0"] * 384) + "]",
                "model": model,
            },
        )
    return sid


async def _skill_model(maker: async_sessionmaker, sid: str) -> str | None:
    async with scoped_session(maker, OWNER) as session:
        return (
            await session.execute(
                text("SELECT embedding_model FROM app.skills WHERE id = cast(:id AS uuid)"),
                {"id": sid},
            )
        ).scalar()


async def test_reembed_restamps_a_stale_skill_and_skips_a_current_one(
    maker: async_sessionmaker,
) -> None:
    stale = await _skill(maker, model="old-model")
    current = await _skill(maker, model="test-model")
    await reembed_handler(maker, embedder=FakeEmbed(), embedding_model="test-model")({})
    assert await _skill_model(maker, stale) == "test-model"  # re-embedded + restamped
    assert await _skill_model(maker, current) == "test-model"  # untouched (already current)


async def test_reembed_restamps_an_entity_with_a_summary_but_skips_a_null_summary(
    maker: async_sessionmaker,
) -> None:
    with_summary = await _entity(maker, status="confirmed")
    no_summary = await _entity(maker, status="confirmed")
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "UPDATE app.entities SET summary = 'a summary', embedding_model = 'old'"
                " WHERE id = cast(:id AS uuid)"
            ),
            {"id": with_summary},
        )
    await reembed_handler(maker, embedder=FakeEmbed(), embedding_model="test-model")({})
    async with scoped_session(maker, OWNER) as session:
        restamped = (
            await session.execute(
                text("SELECT embedding_model FROM app.entities WHERE id = cast(:id AS uuid)"),
                {"id": with_summary},
            )
        ).scalar()
        untouched = (
            await session.execute(
                text("SELECT embedding_model FROM app.entities WHERE id = cast(:id AS uuid)"),
                {"id": no_summary},
            )
        ).scalar()
    assert restamped == "test-model"
    assert untouched is None  # a NULL-summary entity has nothing to embed — left alone


# --- tag_consolidate ------------------------------------------------------


async def _note_with_tags(
    maker: async_sessionmaker, tags: list[str], *, domain: str = "general"
) -> str:
    note = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, :d, 'b')"
            ),
            {"id": note, "cid": uuid.uuid4().hex, "d": domain},
        )
        await session.execute(
            text(
                "INSERT INTO app.note_analysis (note_id, title, tags, domain_code)"
                " VALUES (:n, 't', cast(:tags AS text[]), :d)"
            ),
            {"n": note, "tags": "{" + ",".join(f'"{t}"' for t in tags) + "}", "d": domain},
        )
    return note


async def _tags(maker: async_sessionmaker, note: str) -> list[str]:
    async with scoped_session(maker, OWNER) as session:
        tags = (
            await session.execute(
                text("SELECT tags FROM app.note_analysis WHERE note_id = cast(:n AS uuid)"),
                {"n": note},
            )
        ).scalar()
        return list(tags or [])


async def test_tag_consolidate_folds_case_and_whitespace_duplicates(
    maker: async_sessionmaker,
) -> None:
    note = await _note_with_tags(maker, ["Medication", "medication ", "MEDICATION", "labs"])
    await tag_consolidate_handler(maker)({})
    assert await _tags(maker, note) == ["labs", "medication"]


async def test_tag_consolidate_drops_empty_tags(maker: async_sessionmaker) -> None:
    note = await _note_with_tags(maker, ["  ", "Care", "care"])
    await tag_consolidate_handler(maker)({})
    assert await _tags(maker, note) == ["care"]


async def test_tag_consolidate_is_idempotent(maker: async_sessionmaker) -> None:
    note = await _note_with_tags(maker, ["A", "a"])
    await tag_consolidate_handler(maker)({})
    first = await _tags(maker, note)
    await tag_consolidate_handler(maker)({})  # already canonical → no further change
    assert await _tags(maker, note) == first == ["a"]
