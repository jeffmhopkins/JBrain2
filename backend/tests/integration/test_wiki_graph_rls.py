"""Migration 0046 against real Postgres: the wiki graph-coupled write layer (CLAUDE.md rule 3).

- `wiki_citations` is owner + single-domain RLS, and a BEFORE trigger enforces the contract
  firewall: citation.domain = section.domain = chunk.domain (= fact.domain when fact-backed)
  and citation.note_id = chunk.note_id — so a cross-domain citation can't be created or read.
- a fact-backed citation's `fact_id` goes NULL when the fact is deleted (chunk-only survives).
- `wiki_links` is owner + single-domain RLS; its domain must equal its source section's.
- `entities.wiki_built` defaults false and is flipped back to false in Postgres on any fact /
  mention / identity change, while the builder's mark-clean (wiki_built-only update) sticks.
- the `wiki_source_exclusions.fact_id` FK cascades when the fact is purged.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
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


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as s:
        return str(
            (await s.execute(text("SELECT id FROM app.principals WHERE kind='owner'"))).scalar()
        )


async def _seed(maker: async_sessionmaker, domain: str) -> dict[str, str]:
    """One note/chunk/entity/fact/article/section/revision, all in `domain`, as OWNER."""
    ids = {n: str(uuid.uuid4()) for n in ("note", "chunk", "entity", "fact", "article", "section")}
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:i, :c, :d, 'body')"
            ),
            {"i": ids["note"], "c": ids["note"][:12], "d": domain},
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:i, :n, :d, 'paragraph', 0, 'body')"
            ),
            {"i": ids["chunk"], "n": ids["note"], "d": domain},
        )
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:i, 'Person', 'Subj', :d)"
            ),
            {"i": ids["entity"], "d": domain},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, assertion,"
                " reported_at, note_id, chunk_id, extractor, prompt_version, domain_code)"
                " VALUES (:i, :e, 'p', 'state', 'stmt', 'asserted', '2026-01-01T00:00:00Z',"
                " :n, :c, 'fake', 'v1', :d)"
            ),
            {
                "i": ids["fact"],
                "e": ids["entity"],
                "n": ids["note"],
                "c": ids["chunk"],
                "d": domain,
            },
        )
        ids["article"] = str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.wiki_articles (title, slug) VALUES ('A', :sl) RETURNING id"
                    ),
                    {"sl": f"a-{uuid.uuid4().hex[:8]}"},
                )
            ).scalar()
        )
        ids["section"] = str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.wiki_sections (article_id, domain_code)"
                        " VALUES (:a, :d) RETURNING id"
                    ),
                    {"a": ids["article"], "d": domain},
                )
            ).scalar()
        )
        ids["revision"] = str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.wiki_revisions (section_id, seq, body)"
                        " VALUES (:s, 1, 'rev') RETURNING id"
                    ),
                    {"s": ids["section"]},
                )
            ).scalar()
        )
    return ids


async def _insert_citation(maker, ctx, ids: dict[str, str], **over) -> None:
    payload = {
        "r": ids["revision"],
        "f": over.get("fact_id", ids["fact"]),
        "c": over.get("chunk_id", ids["chunk"]),
        "n": over.get("note_id", ids["note"]),
        "d": over.get("domain_code", ids["domain"]),
    }
    async with scoped_session(maker, ctx) as s:
        await s.execute(
            text(
                "INSERT INTO app.wiki_citations (revision_id, fact_id, chunk_id, note_id,"
                " domain_code) VALUES (:r, :f, :c, :n, :d)"
            ),
            payload,
        )


# ---- citations: RLS + the firewall trigger -------------------------------------------------


async def test_citation_owner_and_domain_narrowed(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    ids = await _seed(maker, "health")
    ids["domain"] = "health"
    await _insert_citation(maker, OWNER, ids)
    cnt = text("SELECT count(*) FROM app.wiki_citations WHERE revision_id = :r")
    async with scoped_session(maker, read_context(pid, ("health",))) as s:
        assert (await s.execute(cnt, {"r": ids["revision"]})).scalar() == 1
    async with scoped_session(maker, read_context(pid, ("general",))) as s:
        assert (await s.execute(cnt, {"r": ids["revision"]})).scalar() == 0
    token = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    async with scoped_session(maker, token) as s:
        assert (await s.execute(cnt, {"r": ids["revision"]})).scalar() == 0


async def test_citation_domain_must_match_section(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "health")
    ids["domain"] = "general"  # citation claims general, section is health
    with pytest.raises(DBAPIError):
        await _insert_citation(maker, OWNER, ids)


async def test_citation_domain_must_match_chunk(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "health")
    other = await _seed(maker, "general")  # a general chunk under its own general note
    ids["domain"] = "health"
    with pytest.raises(DBAPIError):
        await _insert_citation(maker, OWNER, ids, chunk_id=other["chunk"], note_id=other["note"])


async def test_citation_note_must_match_chunk_note(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "health")
    other = await _seed(maker, "health")  # same domain, different note
    ids["domain"] = "health"
    with pytest.raises(DBAPIError):
        await _insert_citation(maker, OWNER, ids, note_id=other["note"])


async def test_citation_domain_must_match_fact(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "health")
    other = await _seed(maker, "general")  # a general-domain fact
    ids["domain"] = "health"
    with pytest.raises(DBAPIError):
        await _insert_citation(maker, OWNER, ids, fact_id=other["fact"])


async def test_chunk_only_citation_allowed_and_fact_set_null_on_delete(
    maker: async_sessionmaker,
) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "health")
    ids["domain"] = "health"
    # A chunk-only claim (fact_id NULL) is valid.
    await _insert_citation(maker, OWNER, ids, fact_id=None)
    # A fact-backed citation survives the fact's deletion as a chunk-only one (SET NULL).
    await _insert_citation(maker, OWNER, ids)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.facts WHERE id = :f"), {"f": ids["fact"]})
        remaining = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.wiki_citations"
                    " WHERE revision_id = :r AND fact_id IS NULL"
                ),
                {"r": ids["revision"]},
            )
        ).scalar()
    assert remaining == 2


async def test_non_owner_cannot_write_citation(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "general")
    ids["domain"] = "general"
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    with pytest.raises(ProgrammingError):
        await _insert_citation(maker, token, ids)


# ---- links: RLS + the firewall trigger -----------------------------------------------------


async def test_link_owner_and_domain_narrowed(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    ids = await _seed(maker, "finance")
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.wiki_links (from_section_id, to_entity_id, anchor, domain_code)"
                " VALUES (:s, :e, 'Subj', 'finance')"
            ),
            {"s": ids["section"], "e": ids["entity"]},
        )
    cnt = text("SELECT count(*) FROM app.wiki_links WHERE from_section_id = :s")
    async with scoped_session(maker, read_context(pid, ("finance",))) as s:
        assert (await s.execute(cnt, {"s": ids["section"]})).scalar() == 1
    async with scoped_session(maker, read_context(pid, ("general",))) as s:
        assert (await s.execute(cnt, {"s": ids["section"]})).scalar() == 0


async def test_link_domain_must_match_section(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "health")
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.wiki_links (from_section_id, domain_code)"
                    " VALUES (:s, 'general')"  # link claims general, section is health
                ),
                {"s": ids["section"]},
            )


# ---- the entities.wiki_built dirty bit + mark-and-sweep propagation ------------------------


async def _mark_clean(maker: async_sessionmaker, entity_id: str) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.entities SET wiki_built = true WHERE id = :e"), {"e": entity_id}
        )


async def _is_built(maker: async_sessionmaker, entity_id: str) -> bool:
    async with scoped_session(maker, OWNER) as s:
        return bool(
            (
                await s.execute(
                    text("SELECT wiki_built FROM app.entities WHERE id = :e"), {"e": entity_id}
                )
            ).scalar()
        )


async def test_entity_wiki_built_defaults_false(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "general")
    assert await _is_built(maker, ids["entity"]) is False


async def test_mark_clean_sticks_for_non_identity_update(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "general")
    await _mark_clean(maker, ids["entity"])
    # A wiki_built-only update is the builder's mark-clean; it must not self-re-dirty.
    assert await _is_built(maker, ids["entity"]) is True
    # Touching a non-identity column (updated_at) also leaves it clean.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.entities SET updated_at = now() WHERE id = :e"), {"e": ids["entity"]}
        )
    assert await _is_built(maker, ids["entity"]) is True


async def test_identity_change_dirties_entity(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "general")
    await _mark_clean(maker, ids["entity"])
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.entities SET canonical_name = 'Renamed' WHERE id = :e"),
            {"e": ids["entity"]},
        )
    assert await _is_built(maker, ids["entity"]) is False


async def test_fact_change_dirties_entity(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "general")
    await _mark_clean(maker, ids["entity"])
    # A new fact for the entity re-dirties it.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, assertion,"
                " reported_at, note_id, chunk_id, extractor, prompt_version, domain_code)"
                " VALUES (:i, :e, 'p2', 'state', 's2', 'asserted', '2026-01-02T00:00:00Z',"
                " :n, :c, 'fake', 'v1', 'general')"
            ),
            {"i": str(uuid.uuid4()), "e": ids["entity"], "n": ids["note"], "c": ids["chunk"]},
        )
    assert await _is_built(maker, ids["entity"]) is False
    # Deleting a fact also dirties (the purge path).
    await _mark_clean(maker, ids["entity"])
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.facts WHERE id = :f"), {"f": ids["fact"]})
    assert await _is_built(maker, ids["entity"]) is False


async def test_mention_change_dirties_entity(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "general")
    await _mark_clean(maker, ids["entity"])
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entity_mentions (id, entity_id, chunk_id, note_id, surface_text,"
                " char_start, char_end, link_method, domain_code)"
                " VALUES (:i, :e, :c, :n, 'Subj', 0, 4, 'llm', 'general')"
            ),
            {"i": str(uuid.uuid4()), "e": ids["entity"], "c": ids["chunk"], "n": ids["note"]},
        )
    assert await _is_built(maker, ids["entity"]) is False


async def test_mention_repoint_dirties_both_entities(maker: async_sessionmaker) -> None:
    # The entity-merge path repoints mentions in place (UPDATE ... SET entity_id). A
    # mention-only absorbed entity has no facts, so the survivor is dirtied ONLY by the
    # mention UPDATE — both OLD (gone) and NEW (keep) must flip.
    await _owner_pid(maker)
    gone = await _seed(maker, "general")
    keep = await _seed(maker, "general")
    # gone owns a mention but (deliberately) no surviving fact to dirty keep through.
    mention = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entity_mentions (id, entity_id, chunk_id, note_id, surface_text,"
                " char_start, char_end, link_method, domain_code)"
                " VALUES (:i, :e, :c, :n, 'Subj', 0, 4, 'llm', 'general')"
            ),
            {"i": mention, "e": gone["entity"], "c": gone["chunk"], "n": gone["note"]},
        )
    await _mark_clean(maker, gone["entity"])
    await _mark_clean(maker, keep["entity"])
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.entity_mentions SET entity_id = :keep WHERE id = :m"),
            {"keep": keep["entity"], "m": mention},
        )
    assert await _is_built(maker, gone["entity"]) is False
    assert await _is_built(maker, keep["entity"]) is False


async def test_object_entity_dirtied_by_relationship_fact(maker: async_sessionmaker) -> None:
    # Relationship facts (the wiki-graph edges) carry object_entity_id; it must dirty too.
    await _owner_pid(maker)
    ids = await _seed(maker, "general")
    obj = await _seed(maker, "general")
    await _mark_clean(maker, obj["entity"])
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, object_entity_id, predicate, kind,"
                " statement, assertion, reported_at, note_id, chunk_id, extractor,"
                " prompt_version, domain_code)"
                " VALUES (:i, :e, :o, 'knows', 'relationship', 'e knows o', 'asserted',"
                " '2026-01-03T00:00:00Z', :n, :c, 'fake', 'v1', 'general')"
            ),
            {
                "i": str(uuid.uuid4()),
                "e": ids["entity"],
                "o": obj["entity"],
                "n": ids["note"],
                "c": ids["chunk"],
            },
        )
    assert await _is_built(maker, obj["entity"]) is False


async def test_dirty_reaches_out_of_scope_entity_under_narrowed_session(
    maker: async_sessionmaker,
) -> None:
    # The load-bearing security property: the SECURITY DEFINER trigger must dirty an entity the
    # WRITING session can't see. A general entity, a health note/chunk; under a HEALTH-narrowed
    # owner session (which can't see the general entity) insert a health fact citing it — the
    # general entity must still flip dirty. An INVOKER trigger would be RLS-filtered and miss it.
    pid = await _owner_pid(maker)
    general = await _seed(maker, "general")
    health = await _seed(maker, "health")
    await _mark_clean(maker, general["entity"])
    narrowed = read_context(pid, ("health",))
    async with scoped_session(maker, narrowed) as s:
        # Sanity: this session genuinely cannot see the general entity.
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.entities WHERE id = :e"), {"e": general["entity"]}
            )
        ).scalar() == 0
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, assertion,"
                " reported_at, note_id, chunk_id, extractor, prompt_version, domain_code)"
                " VALUES (:i, :e, 'p', 'state', 's', 'asserted', '2026-01-04T00:00:00Z',"
                " :n, :c, 'fake', 'v1', 'health')"
            ),
            {
                "i": str(uuid.uuid4()),
                "e": general["entity"],
                "n": health["note"],
                "c": health["chunk"],
            },
        )
    assert await _is_built(maker, general["entity"]) is False


# ---- the fact_id exclusion FK --------------------------------------------------------------


async def test_exclusion_fact_fk_cascades_on_purge(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    ids = await _seed(maker, "health")
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.wiki_source_exclusions (fact_id, domain_code, reason)"
                " VALUES (:f, 'health', 'noisy fact')"
            ),
            {"f": ids["fact"]},
        )
        await s.execute(text("DELETE FROM app.facts WHERE id = :f"), {"f": ids["fact"]})
        remaining = (
            await s.execute(
                text("SELECT count(*) FROM app.wiki_source_exclusions WHERE fact_id = :f"),
                {"f": ids["fact"]},
            )
        ).scalar()
    assert remaining == 0
