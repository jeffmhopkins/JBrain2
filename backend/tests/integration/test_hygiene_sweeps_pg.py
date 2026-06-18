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

from jbrain.agent.session import read_context
from jbrain.analysis.hygiene import entity_hygiene_handler
from jbrain.analysis.purge import sweep_orphaned_entities
from jbrain.analysis.reembed import ReembedAction, reembed_handler
from jbrain.analysis.tagconsolidate import tag_consolidate_handler
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
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
                    "TRUNCATE app.entities, app.skills, app.notes, app.note_analysis, app.subjects"
                    " RESTART IDENTITY CASCADE"
                )
            )
    finally:
        await admin.dispose()


async def _owner_principal(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


async def _entity(
    maker: async_sessionmaker,
    *,
    status: str = "provisional",
    subject: bool = False,
    domain: str = "general",
    age_hours: float = 2.0,
) -> str:
    """Insert an entity `age_hours` old (default 2h, past the sweep's 1h age guard)."""
    eid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        subj_id: str | None = None
        if subject:
            subj_id = str(uuid.uuid4())
            await session.execute(
                text(
                    "INSERT INTO app.subjects (id, display_name, kind) VALUES (:id, 'Me', 'person')"
                ),
                {"id": subj_id},
            )
        await session.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, status, subject_id,"
                " domain_code, created_at)"
                " VALUES (:id, 'person', 'X', :s, :subj, :d,"
                "  now() - cast(:age AS double precision) * interval '1 hour')"
            ),
            {"id": eid, "s": status, "subj": subj_id, "d": domain, "age": age_hours},
        )
    return eid


async def _note(maker: async_sessionmaker, *, domain: str = "general") -> str:
    note = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, :d, 'b')"
            ),
            {"id": note, "cid": uuid.uuid4().hex, "d": domain},
        )
    return note


async def _fact_on(maker: async_sessionmaker, *, subject: str, obj: str | None = None) -> None:
    """A fact citing `subject` as entity_id (and optionally `obj` as object_entity_id)."""
    note = await _note(maker)
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, object_entity_id, predicate, qualifier,"
                " kind, statement, value_json, assertion, reported_at, temporal_precision, status,"
                " note_id, extractor, prompt_version, domain_code)"
                " VALUES (:id, :e, :o, 'p', '', 'state', 's', NULL, 'asserted', now(), 'unknown',"
                "  'active', :n, 'test', 'test-v1', 'general')"
            ),
            {"id": str(uuid.uuid4()), "e": subject, "o": obj, "n": note},
        )


async def _entity_ids(maker: async_sessionmaker) -> set[str]:
    async with scoped_session(maker, OWNER) as session:
        rows = (await session.execute(text("SELECT id::text FROM app.entities"))).scalars()
        return set(rows)


# --- entity_hygiene -------------------------------------------------------


async def test_entity_hygiene_deletes_a_provisional_orphan(maker: async_sessionmaker) -> None:
    orphan = await _entity(maker)
    await entity_hygiene_handler(maker)({})
    assert orphan not in await _entity_ids(maker)


async def test_entity_hygiene_age_guard_spares_a_fresh_orphan(maker: async_sessionmaker) -> None:
    """A provisional orphan younger than the 1h guard is NOT deleted — so a manual sweep
    can't delete an entity an in-flight extraction just inserted but not yet linked."""
    fresh = await _entity(maker, age_hours=0.0)
    await entity_hygiene_handler(maker)({})
    assert fresh in await _entity_ids(maker)


async def test_entity_hygiene_keeps_confirmed_and_subject_entities(
    maker: async_sessionmaker,
) -> None:
    confirmed = await _entity(maker, status="confirmed")
    subject = await _entity(maker, subject=True)
    await entity_hygiene_handler(maker)({})
    survivors = await _entity_ids(maker)
    assert confirmed in survivors and subject in survivors


async def test_entity_hygiene_keeps_an_entity_with_a_mention(maker: async_sessionmaker) -> None:
    eid = await _entity(maker)
    note = await _note(maker)
    async with scoped_session(maker, OWNER) as session:
        chunk = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:id, :n, 'general', 'paragraph', 0, 'b')"
            ),
            {"id": chunk, "n": note},
        )
        await session.execute(
            text(
                "INSERT INTO app.entity_mentions (id, entity_id, chunk_id, note_id, surface_text,"
                " char_start, char_end, link_method, domain_code)"
                " VALUES (:id, :e, :c, :n, 'X', 0, 1, 'exact_alias', 'general')"
            ),
            {"id": str(uuid.uuid4()), "e": eid, "c": chunk, "n": note},
        )
    await entity_hygiene_handler(maker)({})
    assert eid in await _entity_ids(maker)


async def test_entity_hygiene_keeps_an_entity_cited_by_a_fact_as_subject_or_object(
    maker: async_sessionmaker,
) -> None:
    """The two fact arms of the orphan criteria — the heart of the safety argument now that
    the sweep runs corpus-wide: an entity a surviving fact references (either side) is kept."""
    subj = await _entity(maker)
    obj = await _entity(maker)
    await _fact_on(maker, subject=subj, obj=obj)
    await entity_hygiene_handler(maker)({})
    survivors = await _entity_ids(maker)
    assert subj in survivors and obj in survivors


async def test_entity_hygiene_keeps_a_distinct_from_peer_and_a_merge_tombstone(
    maker: async_sessionmaker,
) -> None:
    a = await _entity(maker)
    b = await _entity(maker)
    survivor = await _entity(maker, status="confirmed")
    tomb = await _entity(maker)  # provisional, but a tombstone points at... no: tomb IS merged
    async with scoped_session(maker, OWNER) as session:
        # a/b held by a distinct_from edge (canonical order a<b enforced by the DB trigger).
        lo, hi = sorted([a, b])
        await session.execute(
            text(
                "INSERT INTO app.entity_distinctions (id, entity_a, entity_b, reason, domain_code)"
                " VALUES (:id, :a, :b, 'distinct', 'general')"
            ),
            {"id": str(uuid.uuid4()), "a": lo, "b": hi},
        )
        # `tomb` is a merged tombstone pointing at the survivor — never deleted (un-merge needs it).
        await session.execute(
            text(
                "UPDATE app.entities SET status = 'merged', merged_into_id = cast(:s AS uuid)"
                " WHERE id = cast(:t AS uuid)"
            ),
            {"s": survivor, "t": tomb},
        )
    await entity_hygiene_handler(maker)({})
    survivors = await _entity_ids(maker)
    assert {a, b, survivor, tomb} <= survivors


async def test_entity_hygiene_is_idempotent(maker: async_sessionmaker) -> None:
    await _entity(maker)
    await entity_hygiene_handler(maker)({})
    await entity_hygiene_handler(maker)({})  # second run finds nothing, no error
    assert await _entity_ids(maker) == set()


async def test_entity_hygiene_is_domain_firewalled(maker: async_sessionmaker) -> None:
    """RLS on the delete path: a health-narrowed sweep can only delete in-scope orphans —
    a finance orphan is invisible to it and survives (CLAUDE.md #3, firewall in Postgres)."""
    pid = await _owner_principal(maker)
    health = await _entity(maker, domain="health")
    finance = await _entity(maker, domain="finance")
    await sweep_orphaned_entities(maker, ctx=read_context(pid, ("health",)))
    survivors = await _entity_ids(maker)
    assert health not in survivors and finance in survivors


# --- reembed_stale --------------------------------------------------------


async def _skill(maker: async_sessionmaker, *, model: str | None, domain: str = "general") -> str:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.skills (id, name, version, status, domain_code, body, description,"
                " embedding, embedding_model)"
                " VALUES (:id, :name, 1, 'active', :d, 'do a thing', 'a skill',"
                "  cast(:emb AS vector), :model)"
            ),
            {
                "id": sid,
                "name": f"s-{sid[:8]}",
                "d": domain,
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

    async def _model(eid: str) -> str | None:
        async with scoped_session(maker, OWNER) as session:
            return (
                await session.execute(
                    text("SELECT embedding_model FROM app.entities WHERE id = cast(:id AS uuid)"),
                    {"id": eid},
                )
            ).scalar()

    assert await _model(with_summary) == "test-model"
    assert await _model(no_summary) is None  # NULL summary → nothing to embed → left alone


async def test_reembed_converges_over_runs_with_a_small_batch(maker: async_sessionmaker) -> None:
    """The per-run cap drains a backlog across runs and then no-ops (the convergence claim)."""
    ids = [await _skill(maker, model="old-model") for _ in range(3)]
    action = ReembedAction(maker, embedder=FakeEmbed(), embedding_model="test-model", batch=1)
    await action.run({})  # 1 of 3
    await action.run({})  # 2 of 3
    still_stale = [s for s in ids if await _skill_model(maker, s) != "test-model"]
    assert len(still_stale) == 1
    await action.run({})  # 3 of 3
    assert all([await _skill_model(maker, s) == "test-model" for s in ids])  # noqa: C419


async def test_reembed_is_domain_firewalled(maker: async_sessionmaker) -> None:
    """RLS on the embed-write path: a health-narrowed re-embed leaves a finance skill stale."""
    pid = await _owner_principal(maker)
    health = await _skill(maker, model="old", domain="health")
    finance = await _skill(maker, model="old", domain="finance")
    action = ReembedAction(
        maker,
        embedder=FakeEmbed(),
        embedding_model="test-model",
        ctx=read_context(pid, ("health",)),
    )
    await action.run({})
    assert await _skill_model(maker, health) == "test-model"
    assert await _skill_model(maker, finance) == "old"  # out of scope → untouched


# --- tag_consolidate ------------------------------------------------------


async def _note_with_tags(
    maker: async_sessionmaker, tags: list[str] | None, *, domain: str = "general"
) -> str:
    note = await _note(maker, domain=domain)
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.note_analysis (note_id, title, tags, domain_code)"
                " VALUES (:n, 't', cast(:tags AS text[]), :d)"
            ),
            # asyncpg binds a text[] param from a Python list, not a '{…}' literal.
            {"n": note, "tags": list(tags) if tags is not None else [], "d": domain},
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
        return list(tags if tags is not None else [])


async def test_tag_consolidate_folds_case_and_whitespace_duplicates(
    maker: async_sessionmaker,
) -> None:
    note = await _note_with_tags(maker, ["Medication", "medication ", "MEDICATION", "labs"])
    await tag_consolidate_handler(maker)({})
    assert await _tags(maker, note) == ["labs", "medication"]


async def test_tag_consolidate_drops_empty_and_whitespace_tags(maker: async_sessionmaker) -> None:
    note = await _note_with_tags(maker, ["  ", "Care", "care", "   "])
    await tag_consolidate_handler(maker)({})
    assert await _tags(maker, note) == ["care"]  # empties dropped, NOT a NULL write


async def test_tag_consolidate_collapses_all_whitespace_to_empty_array(
    maker: async_sessionmaker,
) -> None:
    note = await _note_with_tags(maker, [" ", "\t"])
    await tag_consolidate_handler(maker)({})
    assert await _tags(maker, note) == []  # NOT NULL — the column forbids it


async def test_tag_consolidate_leaves_an_empty_array_untouched(maker: async_sessionmaker) -> None:
    note = await _note_with_tags(maker, [])
    await tag_consolidate_handler(maker)({})
    assert await _tags(maker, note) == []


async def test_tag_consolidate_is_idempotent(maker: async_sessionmaker) -> None:
    note = await _note_with_tags(maker, ["A", "a"])
    await tag_consolidate_handler(maker)({})
    first = await _tags(maker, note)
    await tag_consolidate_handler(maker)({})  # already canonical → no further change
    assert await _tags(maker, note) == first == ["a"]
