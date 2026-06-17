"""The wiki builder (Phase 6, Wave C2a) against real Postgres: dirty-bit-driven build →
single-domain sections + append-only revisions + clause citations + links + the embedding
index → mark built. Exercises the whole write path through the migration-0046 firewall with
the deterministic StubRewriter and a faked embed client (no network).

- a notable dirty entity becomes a cited article; the entity is marked built; below-threshold
  entities are skipped (and marked built so they aren't re-scanned).
- a health fact lands a health section + health citation/index (firewall holds; the citation's
  domain = section = chunk).
- a second refresh appends a new revision (history preserved), no duplicate article.
- reindex re-embeds; prune archives an orphaned article.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.queue import SYSTEM_CTX
from jbrain.wiki.builder import StubRewriter, WikiBuilder
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class FakeEmbed:
    """Deterministic 384-dim vectors; records call count so reindex can be observed."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.0] * 384 for _ in texts]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _builder(maker: async_sessionmaker) -> tuple[WikiBuilder, FakeEmbed]:
    embed = FakeEmbed()
    return (
        WikiBuilder(maker, embed=embed, rewriter=StubRewriter(), embedding_model="fake-embed"),
        embed,
    )


async def _entity(maker: async_sessionmaker, domain: str, name: str = "Subj") -> str:
    eid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:i, 'Person', :n, :d)"
            ),
            {"i": eid, "n": name, "d": domain},
        )
    return eid


async def _fact(
    maker: async_sessionmaker,
    entity_id: str,
    domain: str,
    statement: str,
    *,
    object_entity_id: str | None = None,
) -> None:
    """A note + chunk + fact in `domain`, all consistent so the citation firewall accepts it."""
    note, chunk, fact = (str(uuid.uuid4()) for _ in range(3))
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body) VALUES (:i, :c, :d, :b)"
            ),
            {"i": note, "c": note[:12], "d": domain, "b": statement},
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:i, :n, :d, 'paragraph', 0, :b)"
            ),
            {"i": chunk, "n": note, "d": domain, "b": statement},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, object_entity_id, predicate, kind,"
                " statement, assertion, reported_at, note_id, chunk_id, extractor,"
                " prompt_version, domain_code)"
                " VALUES (:i, :e, :o, 'p', 'state', :st, 'asserted', '2026-01-01T00:00:00Z',"
                " :n, :c, 'fake', 'v1', :d)"
            ),
            {
                "i": fact,
                "e": entity_id,
                "o": object_entity_id,
                "st": statement,
                "n": note,
                "c": chunk,
                "d": domain,
            },
        )


async def _ratcheted_fact(
    maker: async_sessionmaker, entity_id: str, *, fact_domain: str, chunk_domain: str
) -> None:
    """A fact whose backing chunk sits in a DIFFERENT domain (the ratcheted case C2a defers):
    the builder's same-domain-chunk join must drop it."""
    note, chunk, fact = (str(uuid.uuid4()) for _ in range(3))
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body) VALUES (:i, :c, :d, 'b')"
            ),
            {"i": note, "c": note[:12], "d": chunk_domain},
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:i, :n, :d, 'paragraph', 0, 'b')"
            ),
            {"i": chunk, "n": note, "d": chunk_domain},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, assertion,"
                " reported_at, note_id, chunk_id, extractor, prompt_version, domain_code)"
                " VALUES (:i, :e, 'p', 'state', 'ratcheted claim', 'asserted',"
                " '2026-01-01T00:00:00Z', :n, :c, 'fake', 'v1', :fd)"
            ),
            {"i": fact, "e": entity_id, "n": note, "c": chunk, "fd": fact_domain},
        )


async def _mention(maker: async_sessionmaker, entity_id: str, domain: str) -> None:
    """A standalone note+chunk+mention of the entity (a mention-only source, no fact)."""
    note, chunk = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body) VALUES (:i, :c, :d, 'b')"
            ),
            {"i": note, "c": note[:12], "d": domain},
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:i, :n, :d, 'paragraph', 0, 'b')"
            ),
            {"i": chunk, "n": note, "d": domain},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_mentions (id, entity_id, chunk_id, note_id, surface_text,"
                " char_start, char_end, link_method, domain_code)"
                " VALUES (:i, :e, :c, :n, 'X', 0, 1, 'llm', :d)"
            ),
            {"i": str(uuid.uuid4()), "e": entity_id, "c": chunk, "n": note, "d": domain},
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


async def test_refresh_builds_a_cited_article_and_marks_built(maker: async_sessionmaker) -> None:
    eid = await _entity(maker, "general", "Priya")
    for i in range(3):  # ≥3 cited facts clears the notability gate
        await _fact(maker, eid, "general", f"claim number {i}")
    builder, embed = _builder(maker)

    processed = await builder.refresh()
    assert processed >= 1
    assert await _is_built(maker, eid) is True
    assert embed.calls > 0  # lead + section summaries embedded

    async with scoped_session(maker, OWNER) as s:
        article = (
            await s.execute(
                text("SELECT id, lead_summary FROM app.wiki_articles WHERE entity_ref = :e"),
                {"e": eid},
            )
        ).first()
        assert article is not None
        assert "Priya" in article.lead_summary
        # One general section, a revision, three citations, an index row.
        sec = (
            await s.execute(
                text("SELECT id, domain_code FROM app.wiki_sections WHERE article_id = :a"),
                {"a": article.id},
            )
        ).first()
        assert sec is not None
        assert sec.domain_code == "general"
        cites = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.wiki_citations c"
                    " JOIN app.wiki_revisions r ON r.id = c.revision_id"
                    " WHERE r.section_id = :s"
                ),
                {"s": sec.id},
            )
        ).scalar()
        assert cites == 3
        idx = (
            await s.execute(
                text("SELECT count(*) FROM app.wiki_index WHERE section_id = :s"), {"s": sec.id}
            )
        ).scalar()
        assert idx == 1


async def test_below_notability_is_skipped_but_marked_built(maker: async_sessionmaker) -> None:
    eid = await _entity(maker, "general", "Minor")
    await _fact(maker, eid, "general", "only one claim")  # 1 fact, 1 note → not notable
    builder, _ = _builder(maker)
    await builder.refresh()
    assert await _is_built(maker, eid) is True  # marked built so it isn't re-scanned
    async with scoped_session(maker, OWNER) as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.wiki_articles WHERE entity_ref = :e"), {"e": eid}
            )
        ).scalar() == 0


async def test_health_fact_yields_a_health_section_and_citation(maker: async_sessionmaker) -> None:
    eid = await _entity(maker, "general", "Mixed")
    await _fact(maker, eid, "general", "general claim a")
    await _fact(maker, eid, "general", "general claim b")
    await _fact(maker, eid, "health", "has a peanut allergy")
    builder, _ = _builder(maker)
    await builder.refresh()
    async with scoped_session(maker, OWNER) as s:
        domains = sorted(
            (
                await s.execute(
                    text(
                        "SELECT s.domain_code FROM app.wiki_sections s"
                        " JOIN app.wiki_articles a ON a.id = s.article_id WHERE a.entity_ref = :e"
                    ),
                    {"e": eid},
                )
            ).scalars()
        )
        assert domains == ["general", "health"]
        # The health citation carries the health domain (firewall held end to end).
        health_cites = (
            await s.execute(
                text("SELECT count(*) FROM app.wiki_citations WHERE domain_code = 'health'")
            )
        ).scalar()
        assert health_cites == 1


async def test_second_refresh_appends_a_revision_not_a_new_article(
    maker: async_sessionmaker,
) -> None:
    eid = await _entity(maker, "general", "Repeat")
    for i in range(3):
        await _fact(maker, eid, "general", f"claim {i}")
    builder, _ = _builder(maker)
    await builder.refresh()
    # Re-dirty and rebuild: a new revision appends; the article is reused.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.entities SET wiki_built = false WHERE id = :e"), {"e": eid}
        )
    await builder.refresh()
    async with scoped_session(maker, OWNER) as s:
        articles = (
            await s.execute(
                text("SELECT count(*) FROM app.wiki_articles WHERE entity_ref = :e"), {"e": eid}
            )
        ).scalar()
        assert articles == 1
        revs = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.wiki_revisions r"
                    " JOIN app.wiki_sections s ON s.id = r.section_id"
                    " JOIN app.wiki_articles a ON a.id = s.article_id WHERE a.entity_ref = :e"
                ),
                {"e": eid},
            )
        ).scalar()
        assert revs == 2  # two builds, two revisions on the one section


async def test_reindex_reembeds_every_index_row(maker: async_sessionmaker) -> None:
    eid = await _entity(maker, "general", "Indexed")
    for i in range(3):
        await _fact(maker, eid, "general", f"claim {i}")
    builder, embed = _builder(maker)
    await builder.refresh()
    before = embed.calls
    count = await builder.reindex()
    assert count >= 1  # module-scoped DB accumulates index rows across tests
    assert embed.calls > before


async def test_cross_domain_chunk_fact_is_skipped(maker: async_sessionmaker) -> None:
    eid = await _entity(maker, "general", "Ratchet")
    for i in range(3):  # three citable same-domain facts → notable, builds
        await _fact(maker, eid, "general", f"claim {i}")
    # A health fact backed by a GENERAL chunk: dropped by the same-domain-chunk join (C2a defers
    # derived-chunk minting to C2b), so no health section and no fourth citation appear.
    await _ratcheted_fact(maker, eid, fact_domain="health", chunk_domain="general")
    builder, _ = _builder(maker)
    await builder.refresh()
    async with scoped_session(maker, OWNER) as s:
        domains = sorted(
            (
                await s.execute(
                    text(
                        "SELECT s.domain_code FROM app.wiki_sections s"
                        " JOIN app.wiki_articles a ON a.id = s.article_id WHERE a.entity_ref = :e"
                    ),
                    {"e": eid},
                )
            ).scalars()
        )
        assert domains == ["general"]  # the health (ratcheted) fact produced no section
        cites = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.wiki_citations c"
                    " JOIN app.wiki_revisions r ON r.id = c.revision_id"
                    " JOIN app.wiki_sections s ON s.id = r.section_id"
                    " JOIN app.wiki_articles a ON a.id = s.article_id WHERE a.entity_ref = :e"
                ),
                {"e": eid},
            )
        ).scalar()
        assert cites == 3


async def test_notable_but_zero_sections_creates_no_article(maker: async_sessionmaker) -> None:
    # Notable only via mentions (two mention-notes, zero citable facts) → no empty article row.
    eid = await _entity(maker, "general", "MentionOnly")
    await _mention(maker, eid, "general")
    await _mention(maker, eid, "general")
    builder, _ = _builder(maker)
    await builder.refresh()
    assert await _is_built(maker, eid) is True  # marked built so it isn't re-scanned
    async with scoped_session(maker, OWNER) as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.wiki_articles WHERE entity_ref = :e"), {"e": eid}
            )
        ).scalar() == 0


async def test_rebuild_redrives_ignoring_the_dirty_bit(maker: async_sessionmaker) -> None:
    eid = await _entity(maker, "general", "Rebuilt")
    for i in range(3):
        await _fact(maker, eid, "general", f"claim {i}")
    builder, _ = _builder(maker)
    await builder.refresh()  # builds + marks clean
    async with scoped_session(maker, OWNER) as s:
        article_id = (
            await s.execute(
                text("SELECT id FROM app.wiki_articles WHERE entity_ref = :e"), {"e": eid}
            )
        ).scalar()
    # The entity is clean, so refresh would skip it — rebuild re-derives anyway, appending a
    # revision. "all" also re-derives every active article.
    assert await builder.rebuild(str(article_id)) == 1
    assert await builder.rebuild("all") >= 1
    async with scoped_session(maker, OWNER) as s:
        revs = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.wiki_revisions r"
                    " JOIN app.wiki_sections s ON s.id = r.section_id WHERE s.article_id = :a"
                ),
                {"a": article_id},
            )
        ).scalar()
        assert revs == 3  # one build + two rebuilds


async def test_action_handlers_drive_the_builder(maker: async_sessionmaker) -> None:
    from jbrain.wiki.actions import wiki_handlers

    eid = await _entity(maker, "general", "ViaAction")
    for i in range(3):
        await _fact(maker, eid, "general", f"claim {i}")
    handlers = wiki_handlers(maker, embed=FakeEmbed(), embedding_model="fake-embed")
    assert set(handlers) == {"wiki_refresh", "wiki_rebuild", "wiki_reindex", "wiki_prune"}
    await handlers["wiki_refresh"]({})
    assert await _is_built(maker, eid) is True
    await handlers["wiki_reindex"]({})
    await handlers["wiki_rebuild"]({"target": "all"})
    await handlers["wiki_prune"]({})


async def test_prune_archives_an_orphaned_article(maker: async_sessionmaker) -> None:
    eid = await _entity(maker, "general", "Doomed")
    for i in range(3):
        await _fact(maker, eid, "general", f"claim {i}")
    builder, _ = _builder(maker)
    await builder.refresh()
    # The anchor entity vanishes (purge); its article is now an orphan.
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(text("DELETE FROM app.facts WHERE entity_id = :e"), {"e": eid})
        await s.execute(text("DELETE FROM app.entities WHERE id = :e"), {"e": eid})
        await s.commit()
    pruned = await builder.prune()
    assert pruned == 1
    async with scoped_session(maker, OWNER) as s:
        status = (
            await s.execute(
                text("SELECT status FROM app.wiki_articles WHERE entity_ref = :e"), {"e": eid}
            )
        ).scalar()
        assert status == "archived"
