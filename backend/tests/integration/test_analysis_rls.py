"""Migration 0006 against real Postgres: RLS isolation for every analysis
table (CLAUDE.md rule 3), the fact supersession chain, and the DB-enforced
uniqueness/ordering constraints the pipeline relies on."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def seed_health_graph(maker: async_sessionmaker) -> dict[str, str]:
    """Insert one health-domain row in every analysis table; return ids.

    Fresh UUIDs per call so parametrized tests never collide in the
    module-scoped database.
    """
    ids = {
        name: str(uuid.uuid4())
        for name in (
            "note",
            "chunk",
            "entity",
            "entity_b",
            "alias",
            "mention",
            "distinction",
            "token",
            "fact",
            "review",
        )
    }
    # entity_distinctions enforces entity_a < entity_b; hex-string order
    # matches Postgres uuid byte order.
    ids["entity"], ids["entity_b"] = sorted((ids["entity"], ids["entity_b"]))

    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, 'health', 'BP 118/76 at Dr. Patel')"
            ),
            {"id": ids["note"], "cid": f"analysis-{ids['note'][:13]}"},
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:id, :nid, 'health', 'paragraph', 0, 'BP 118/76 at Dr. Patel')"
            ),
            {"id": ids["chunk"], "nid": ids["note"]},
        )
        for eid, name in ((ids["entity"], "Dr. Patel"), (ids["entity_b"], "Dr. Patel Jr.")):
            await s.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (:id, 'Person', :name, 'health')"
                ),
                {"id": eid, "name": name},
            )
        await s.execute(
            text(
                "INSERT INTO app.entity_aliases (id, entity_id, alias, alias_norm, domain_code)"
                " VALUES (:id, :eid, 'Dr. Patel', 'dr. patel', 'health')"
            ),
            {"id": ids["alias"], "eid": ids["entity"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_mentions"
                " (id, entity_id, chunk_id, note_id, surface_text,"
                "  char_start, char_end, link_method, domain_code)"
                " VALUES (:id, :eid, :cid, :nid, 'Dr. Patel', 13, 22, 'exact_alias', 'health')"
            ),
            {"id": ids["mention"], "eid": ids["entity"], "cid": ids["chunk"], "nid": ids["note"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_distinctions (id, entity_a, entity_b, reason, domain_code)"
                " VALUES (:id, :a, :b, 'father and son', 'health')"
            ),
            {"id": ids["distinction"], "a": ids["entity"], "b": ids["entity_b"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.temporal_tokens"
                " (id, note_id, chunk_id, surface_phrase, kind, resolved_start,"
                "  temporal_precision, capture_anchor, domain_code)"
                " VALUES (:id, :nid, :cid, 'this morning', 'point',"
                "  '2026-06-10T08:00:00Z', 'day', '2026-06-10T09:00:00Z', 'health')"
            ),
            {"id": ids["token"], "nid": ids["note"], "cid": ids["chunk"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts"
                " (id, entity_id, predicate, kind, statement, assertion, reported_at,"
                "  temporal_token_id, note_id, chunk_id, extractor, prompt_version,"
                "  confidence, domain_code)"
                " VALUES (:id, :eid, 'bloodPressure', 'measurement',"
                "  'BP was 118/76 this morning', 'asserted', '2026-06-10T09:00:00Z',"
                "  :tid, :nid, :cid, 'fake-model', 'v1', 0.95, 'health')"
            ),
            {
                "id": ids["fact"],
                "eid": ids["entity"],
                "tid": ids["token"],
                "nid": ids["note"],
                "cid": ids["chunk"],
            },
        )
        await s.execute(
            text(
                "INSERT INTO app.review_items (id, kind, payload, domain_code)"
                " VALUES (:id, 'merge_proposal', :payload, 'health')"
            ),
            {"id": ids["review"], "payload": '{"entity_ids": ["' + ids["entity"] + '"]}'},
        )
    return ids


async def count_visible(
    maker: async_sessionmaker, ctx: SessionContext, table: str, row_id: str
) -> int:
    async with scoped_session(maker, ctx) as s:
        result = await s.execute(
            text(f"SELECT count(*) FROM app.{table} WHERE id = :id"),
            {"id": row_id},
        )
        return result.scalar_one()


@pytest.mark.parametrize(
    ("table", "id_key"),
    [
        ("entities", "entity"),
        ("entity_aliases", "alias"),
        ("entity_mentions", "mention"),
        ("entity_distinctions", "distinction"),
        ("temporal_tokens", "token"),
        ("facts", "fact"),
        ("review_items", "review"),
    ],
)
async def test_analysis_tables_enforce_domain_firewall(
    maker: async_sessionmaker, table: str, id_key: str
) -> None:
    """A health-scoped row is invisible without the health scope (rule 3)."""
    ids = await seed_health_graph(maker)
    assert await count_visible(maker, HEALTH_ONLY, table, ids[id_key]) == 1
    assert await count_visible(maker, OWNER, table, ids[id_key]) == 1
    assert await count_visible(maker, GENERAL_ONLY, table, ids[id_key]) == 0
    assert await count_visible(maker, UNSCOPED, table, ids[id_key]) == 0


async def test_scoped_writer_cannot_smuggle_analysis_rows_across_domains(
    maker: async_sessionmaker,
) -> None:
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), 'Person', 'Sneaky', 'health')"
                )
            )
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.review_items (id, kind, domain_code)"
                    " VALUES (gen_random_uuid(), 'fact_conflict', 'health')"
                )
            )


async def test_derived_inverse_edge_obeys_domain_firewall(
    maker: async_sessionmaker,
) -> None:
    """A derived inverse edge inherits its SOURCE fact's domain, so a
    health-domain reciprocal is invisible to a general-only scope (Issue 2:
    the derived row is a security-firewalled fact like any other)."""
    ids = await seed_health_graph(maker)
    derived = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        # The seeded fact is the source; its reciprocal lands on entity_b,
        # carrying the source's health domain via derived_from_fact_id.
        await s.execute(
            text(
                "INSERT INTO app.facts"
                " (id, entity_id, predicate, kind, statement, assertion, reported_at,"
                "  object_entity_id, derived_from_fact_id, note_id, extractor,"
                "  prompt_version, domain_code)"
                " VALUES (:id, :eid, 'treatedBy', 'relationship',"
                "  'derived reciprocal', 'asserted', now(), :obj, :src, :nid,"
                "  'fake-model', 'v1', 'health')"
            ),
            {
                "id": derived,
                "eid": ids["entity_b"],
                "obj": ids["entity"],
                "src": ids["fact"],
                "nid": ids["note"],
            },
        )
    assert await count_visible(maker, HEALTH_ONLY, "facts", derived) == 1
    assert await count_visible(maker, OWNER, "facts", derived) == 1
    assert await count_visible(maker, GENERAL_ONLY, "facts", derived) == 0
    assert await count_visible(maker, UNSCOPED, "facts", derived) == 0


async def test_inverse_proposal_review_item_obeys_domain_firewall(
    maker: async_sessionmaker,
) -> None:
    """A cross-subject inverse is PROPOSED, never written: the proposal is an
    inverse_proposal review item carrying the SOURCE fact's domain, so it sits
    behind the same firewall (Issue 2, Phase D — the cross-subject gate)."""
    ids = await seed_health_graph(maker)
    proposal = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.review_items (id, kind, payload, domain_code)"
                " VALUES (:id, 'inverse_proposal', :payload, 'health')"
            ),
            {"id": proposal, "payload": '{"source_fact_id": "' + ids["fact"] + '"}'},
        )
    assert await count_visible(maker, HEALTH_ONLY, "review_items", proposal) == 1
    assert await count_visible(maker, GENERAL_ONLY, "review_items", proposal) == 0
    assert await count_visible(maker, UNSCOPED, "review_items", proposal) == 0


async def test_supersession_chain_walks_and_preserves_history(
    maker: async_sessionmaker,
) -> None:
    """Superseding never deletes: the chain IS the revision history."""
    ids = await seed_health_graph(maker)
    fact_a, fact_b, fact_c = ids["fact"], str(uuid.uuid4()), str(uuid.uuid4())

    async with scoped_session(maker, OWNER) as s:
        for fid, statement in ((fact_b, "BP was 121/79"), (fact_c, "BP was 117/75")):
            await s.execute(
                text(
                    "INSERT INTO app.facts"
                    " (id, entity_id, predicate, kind, statement, assertion, reported_at,"
                    "  note_id, extractor, prompt_version, domain_code)"
                    " VALUES (:id, :eid, 'bloodPressure', 'measurement', :stmt, 'asserted',"
                    "  now(), :nid, 'fake-model', 'v1', 'health')"
                ),
                {"id": fid, "eid": ids["entity"], "stmt": statement, "nid": ids["note"]},
            )
        for old, new in ((fact_a, fact_b), (fact_b, fact_c)):
            await s.execute(
                text(
                    "UPDATE app.facts SET status = 'superseded', superseded_by = :new"
                    " WHERE id = :old"
                ),
                {"old": old, "new": new},
            )

    async with scoped_session(maker, OWNER) as s:
        chain = list(
            (
                await s.execute(
                    text(
                        """
                        WITH RECURSIVE chain AS (
                            SELECT id, superseded_by, status, 0 AS depth
                            FROM app.facts WHERE id = :start
                            UNION ALL
                            SELECT f.id, f.superseded_by, f.status, chain.depth + 1
                            FROM app.facts f JOIN chain ON f.id = chain.superseded_by
                        )
                        SELECT id::text, status FROM chain ORDER BY depth
                        """
                    ),
                    {"start": fact_a},
                )
            ).all()
        )
    assert [(row[0], row[1]) for row in chain] == [
        (fact_a, "superseded"),
        (fact_b, "superseded"),
        (fact_c, "active"),
    ]
    # The superseded original is still readable with full provenance.
    assert await count_visible(maker, HEALTH_ONLY, "facts", fact_a) == 1


async def test_duplicate_alias_norm_rejected(maker: async_sessionmaker) -> None:
    ids = await seed_health_graph(maker)
    with pytest.raises(IntegrityError):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.entity_aliases"
                    " (id, entity_id, alias, alias_norm, domain_code)"
                    " VALUES (gen_random_uuid(), :eid, 'DR. PATEL', 'dr. patel', 'health')"
                ),
                {"eid": ids["entity"]},
            )


async def test_distinction_pair_ordering_and_uniqueness_enforced(
    maker: async_sessionmaker,
) -> None:
    ids = await seed_health_graph(maker)
    # Reversed pair violates the entity_a < entity_b canonical ordering.
    with pytest.raises(IntegrityError):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.entity_distinctions (id, entity_a, entity_b, domain_code)"
                    " VALUES (gen_random_uuid(), :a, :b, 'health')"
                ),
                {"a": ids["entity_b"], "b": ids["entity"]},
            )
    # The same canonical pair can never be recorded twice.
    with pytest.raises(IntegrityError):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.entity_distinctions (id, entity_a, entity_b, domain_code)"
                    " VALUES (gen_random_uuid(), :a, :b, 'health')"
                ),
                {"a": ids["entity"], "b": ids["entity_b"]},
            )
