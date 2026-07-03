"""The `wiki_lint` corpus-health sweep (Phase-6 follow-on, docs/plans/WIKI_LINT_PLAN.md) — Wave A,
deterministic slice — against real Postgres. No LLM; the builder writes with the StubRewriter and a
faked embed client.

Covers the plan's mandatory Wave-A checks and the convergence/security guarantees:
- check 3 coverage gaps: a notable entity without an article is counted; the deliberate
  notable-but-sectionless class (notable via mentions, zero citable facts) is suppressed.
- check 5a red-link-became-notable + 5b stale-missing-inbound: counted to the report, NEVER
  re-dirtied (both non-convergent against the production LLM rewriter).
- check 4 missing cross-references: fact-backed (4a) and bare co-mention (4b) counted, neither
  re-dirtied.
- index-integrity: the ONLY re-dirty leg — a buildable entity's stale/missing index re-dirties;
  a non-buildable (sectionless) entity with a stale section is EXCLUDED (no expensive-rebuild loop).
- second run over an unchanged corpus re-dirties nothing (convergence).
- the fact-backed-citation invariant guard (defends the check-6 drop): no builder citation is
  null-fact_id.
- SECURITY PATH (100%): the per-arm firewall uses each endpoint's OWN entities.domain_code, so a
  two-distinct-restricted pair is dropped even when both mentions share a general chunk domain —
  proving the entity-row key governs, not the mention-row domain.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.wiki.builder import StubRewriter, WikiBuilder
from jbrain.wiki.lint import WikiLinter
from tests.conftest import docker_available
from tests.integration.test_rls import APP_PASSWORD, OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class FakeEmbed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# The `database_url` fixture is module-scoped (one migrated DB shared across the module), but the
# linter computes CORPUS-WIDE counts — so each test needs a clean slate or a prior test's rows
# inflate the counts. `notes`/`facts`/`entities` are not app-role-deletable (privileged purge
# path), so truncate via the admin role with CASCADE (the test_hygiene_sweeps_pg pattern), which
# clears every FK-dependent (chunks, mentions, facts, wiki_* incl. talk) in one shot.
@pytest.fixture(autouse=True)
async def _clean(database_url: str) -> AsyncIterator[None]:  # noqa: F811
    admin = create_async_engine(
        database_url.replace(f"jbrain_app:{APP_PASSWORD}", "test:test"), poolclass=NullPool
    )
    try:
        async with admin.begin() as conn:
            await conn.execute(
                text("TRUNCATE app.entities, app.notes, app.wiki_articles CASCADE")
            )
    finally:
        await admin.dispose()
    yield


def _linter(maker: async_sessionmaker, *, redirty_index: bool = True) -> WikiLinter:
    return WikiLinter(maker, embedding_model="fake-embed", redirty_index=redirty_index)


async def _build(maker: async_sessionmaker) -> None:
    """Build every currently-dirty notable entity with the deterministic StubRewriter."""
    builder = WikiBuilder(
        maker, embed=FakeEmbed(), rewriter=StubRewriter(), embedding_model="fake-embed"
    )
    await builder.refresh()


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
    """A note + chunk + fact in `domain` (firewall-consistent)."""
    note, chunk, fact = (str(uuid.uuid4()) for _ in range(3))
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:i, :c, :d, :b)"
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
            {"i": fact, "e": entity_id, "o": object_entity_id, "st": statement, "n": note,
             "c": chunk, "d": domain},
        )


async def _mention(maker: async_sessionmaker, entity_id: str, domain: str) -> None:
    """A standalone note+chunk+mention of one entity (a mention-only source, no fact)."""
    note, chunk = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:i, :c, :d, 'b')"
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


async def _comention(maker: async_sessionmaker, e1: str, e2: str, chunk_domain: str) -> None:
    """Two entities mentioned in the SAME chunk (a co-mention). The chunk carries `chunk_domain`,
    which is what `entity_mentions.domain_code` records — it may diverge from either entity's own
    `entities.domain_code` (the divergence the firewall security test exploits)."""
    note, chunk = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:i, :c, :d, 'b')"
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
        for eid in (e1, e2):
            await s.execute(
                text(
                    "INSERT INTO app.entity_mentions (id, entity_id, chunk_id, note_id,"
                    " surface_text, char_start, char_end, link_method, domain_code)"
                    " VALUES (:i, :e, :c, :n, 'X', 0, 1, 'llm', :d)"
                ),
                {"i": str(uuid.uuid4()), "e": eid, "c": chunk, "n": note, "d": chunk_domain},
            )


async def _built(maker: async_sessionmaker, entity_id: str) -> bool:
    async with scoped_session(maker, OWNER) as s:
        return bool(
            (
                await s.execute(
                    text("SELECT wiki_built FROM app.entities WHERE id = :e"), {"e": entity_id}
                )
            ).scalar()
        )


# ---- check 3: coverage gaps ---------------------------------------------------------------


async def test_coverage_gap_counts_notable_without_article_suppresses_sectionless(
    maker: async_sessionmaker,
) -> None:
    # E3: notable and built → NOT a gap (has an article). Build it first so refresh doesn't also
    # build the gap entity.
    e3 = await _entity(maker, "general", "Built")
    for i in range(3):
        await _fact(maker, e3, "general", f"e3 claim {i}")
    await _build(maker)

    # E1: notable (3 facts), no article, ≥1 citable fact → a REAL coverage gap.
    e1 = await _entity(maker, "general", "Gap")
    for i in range(3):
        await _fact(maker, e1, "general", f"e1 claim {i}")

    # E2: notable via 2 mention-notes but ZERO citable facts → the deliberate sectionless class,
    # suppressed by the default ≥1-citable-fact filter.
    e2 = await _entity(maker, "general", "Sectionless")
    await _mention(maker, e2, "general")
    await _mention(maker, e2, "general")

    report = await _linter(maker).run()
    assert report.coverage_gaps == 1  # E1 only; E2 suppressed, E3 has an article


# ---- check 5a / 5b: report-only, never re-dirtied ------------------------------------------


async def test_redlink_became_notable_counted_not_redirtied(maker: async_sessionmaker) -> None:
    b = await _entity(maker, "general", "Target")
    a = await _entity(maker, "general", "Source")
    # A is notable (3 facts), one of which is a relationship fact A→B; B has no article yet, so A's
    # section links to B as a RED-link.
    await _fact(maker, a, "general", "a plain 1")
    await _fact(maker, a, "general", "a plain 2")
    await _fact(maker, a, "general", "a knows b", object_entity_id=b)
    await _build(maker)  # builds A (notable); B is dirty-but-not-notable → marked built, no article
    assert await _built(maker, a) is True

    # B becomes notable and gets an article; A is NOT rebuilt, so its red-link to B stays stale.
    for i in range(3):
        await _fact(maker, b, "general", f"b claim {i}")
    await _build(maker)  # builds B; A unchanged (still built) → red-link survives

    report = await _linter(maker).run()
    assert report.redlink_became_notable >= 1
    assert await _built(maker, a) is True  # 5a is Talk/report-only — A is NEVER re-dirtied


async def test_stale_missing_inbound_counted_not_redirtied(maker: async_sessionmaker) -> None:
    t = await _entity(maker, "general", "Hub")
    s = await _entity(maker, "general", "Linker")
    for i in range(3):
        await _fact(maker, t, "general", f"t claim {i}")
    # S holds a relationship fact toward T but has no article (not notable) → no inbound wiki_link
    # into T, yet a live source fact exists → 5b.
    await _fact(maker, s, "general", "s points at t", object_entity_id=t)
    await _build(maker)  # builds T (has an article); S not notable → no article, no link into T
    assert await _built(maker, t) is True

    report = await _linter(maker).run()
    assert report.stale_missing_inbound >= 1
    assert await _built(maker, t) is True  # 5b is report-only — never re-dirtied


# ---- check 4: missing cross-references -----------------------------------------------------


async def test_missing_xref_fact_backed_and_bare_counted_not_redirtied(
    maker: async_sessionmaker,
) -> None:
    # 4a: co-mentioned + a relationship fact, no wiki link (no articles built).
    a = await _entity(maker, "general", "A")
    b = await _entity(maker, "general", "B")
    await _comention(maker, a, b, "general")
    await _fact(maker, a, "general", "a knows b", object_entity_id=b)
    # 4b: co-mentioned, NO relationship fact, no link.
    c = await _entity(maker, "general", "C")
    d = await _entity(maker, "general", "D")
    await _comention(maker, c, d, "general")

    report = await _linter(maker, redirty_index=False).run()
    assert report.missing_xref_fact_backed == 1  # (A,B)
    assert report.missing_xref_bare_comention == 1  # (C,D)
    # Nothing in check 4 re-dirties (redirty_index=False anyway; assert the report field too).
    assert report.redirtied == 0


# ---- SECURITY PATH (100%): the per-arm firewall uses the ENTITY-row domain -----------------


async def test_firewall_drops_two_restricted_pair_via_entity_row_not_mention_row(
    maker: async_sessionmaker,
) -> None:
    # A definitely-counted general pair: (Ga, Gb) co-mentioned + relationship fact → 4a == 1.
    ga = await _entity(maker, "general", "Ga")
    gb = await _entity(maker, "general", "Gb")
    await _comention(maker, ga, gb, "general")
    await _fact(maker, ga, "general", "ga knows gb", object_entity_id=gb)

    # A two-distinct-restricted pair co-mentioned in a GENERAL chunk: the MENTION domain is
    # 'general' for both arms (would wrongly pass a mention-keyed filter), but the ENTITY-row
    # domains are health/finance → the entity-row firewall DROPS the pair. It must NOT be counted.
    h = await _entity(maker, "health", "H")
    f = await _entity(maker, "finance", "F")
    # mention domain 'general' for both arms; entity-row domains health/finance
    await _comention(maker, h, f, "general")
    await _fact(maker, h, "health", "h relates f", object_entity_id=f)

    report = await _linter(maker, redirty_index=False).run()
    # Exactly the general pair is counted. If the impl keyed on the mention domain, the health×
    # finance pair (mention='general') would leak in and this would be 2 — proving the entity-row
    # key governs AND the two-restricted firewall drop both hold.
    assert report.missing_xref_fact_backed == 1


# ---- index-integrity: the only re-dirty leg (convergence guarantee) ------------------------


async def test_index_redirties_buildable_but_excludes_sectionless(
    maker: async_sessionmaker,
) -> None:
    # A normal buildable entity: article + section + index. Corrupt (delete) its index row → a
    # missing-index problem on a section that WILL reappear on rebuild → re-dirty converges.
    e = await _entity(maker, "general", "Buildable")
    for i in range(3):
        await _fact(maker, e, "general", f"e claim {i}")
    await _build(maker)
    assert await _built(maker, e) is True

    # A NON-buildable entity that nonetheless carries a section with a (missing) index row: notable
    # only via mentions (zero citable facts), hand-given an article+section to model the
    # orphaned/sectionless residue. It must be EXCLUDED from the re-dirty (else an unbounded
    # expensive-rebuild loop, since a rebuild re-derives zero sections for it).
    s = await _entity(maker, "general", "Sectionless")
    await _mention(maker, s, "general")
    await _mention(maker, s, "general")
    async with scoped_session(maker, OWNER) as sess:
        art = (
            await sess.execute(
                text(
                    "INSERT INTO app.wiki_articles (entity_ref, title, kind, slug)"
                    " VALUES (:e, 'Sectionless', 'Person', :sl) RETURNING id"
                ),
                {"e": s, "sl": f"sectionless-{uuid.uuid4().hex[:6]}"},
            )
        ).scalar()
        await sess.execute(
            text(
                "INSERT INTO app.wiki_sections (article_id, domain_code, heading, seq)"
                # no wiki_index row for this section → a missing-index problem
                " VALUES (:a, 'general', 'Overview', 0)"
            ),
            {"a": art},
        )
        # Mark it built so a stray re-dirty is observable as a flip back to false.
        await sess.execute(
            text("UPDATE app.entities SET wiki_built = true WHERE id = :e"), {"e": s}
        )

    # Delete the buildable entity's index row(s) → its missing-index problem.
    async with scoped_session(maker, OWNER) as sess:
        await sess.execute(
            text(
                "DELETE FROM app.wiki_index WHERE section_id IN ("
                " SELECT sec.id FROM app.wiki_sections sec"
                " JOIN app.wiki_articles a ON a.id = sec.article_id WHERE a.entity_ref = :e)"
            ),
            {"e": e},
        )

    report = await _linter(maker, redirty_index=True).run()
    assert report.index_problems >= 2  # both sections have a missing index row
    assert report.redirtied == 1  # only the buildable entity
    assert await _built(maker, e) is False  # buildable → re-dirtied (converges next refresh)
    assert await _built(maker, s) is True  # sectionless residue → EXCLUDED (no loop)


async def test_second_run_over_unchanged_corpus_redirties_nothing(
    maker: async_sessionmaker,
) -> None:
    e = await _entity(maker, "general", "Stable")
    for i in range(3):
        await _fact(maker, e, "general", f"claim {i}")
    await _build(maker)  # fresh index rows, model matches → no index problem

    first = await _linter(maker).run()
    assert first.redirtied == 0
    second = await _linter(maker).run()
    assert second.redirtied == 0  # convergence: an unchanged corpus never re-dirties


# ---- fact-backed-citation invariant guard (defends the check-6 drop) -----------------------


async def test_builder_never_writes_a_null_fact_id_citation(maker: async_sessionmaker) -> None:
    e = await _entity(maker, "general", "Cited")
    for i in range(3):
        await _fact(maker, e, "general", f"claim {i}")
    await _build(maker)
    async with scoped_session(maker, OWNER) as s:
        nulls = (
            await s.execute(text("SELECT count(*) FROM app.wiki_citations WHERE fact_id IS NULL"))
        ).scalar()
    # The check-6 drop (dangling-[n] is self-healing via the fact trigger) rests on every citation
    # being fact-backed. If a chunk-only citation path is ever introduced this goes red and check 6
    # is re-scoped (docs/plans/WIKI_LINT_PLAN.md §2 check 6).
    assert nulls == 0


async def test_already_linked_comention_pair_is_excluded(maker: async_sessionmaker) -> None:
    # A is notable (3 facts, one a relationship fact A→B) and builds an article whose section links
    # to B — so a wiki_link A→B EXISTS (a red-link, since B has no article). A and B are also
    # co-mentioned. The existing link means the pair is NOT a missing cross-reference.
    b = await _entity(maker, "general", "B")
    a = await _entity(maker, "general", "A")
    await _fact(maker, a, "general", "a plain 1")
    await _fact(maker, a, "general", "a plain 2")
    await _fact(maker, a, "general", "a knows b", object_entity_id=b)
    await _comention(maker, a, b, "general")
    await _build(maker)  # A builds a section with a (red) wiki_link to B

    report = await _linter(maker, redirty_index=False).run()
    assert report.missing_xref_fact_backed == 0  # (A,B) already linked → excluded
    assert report.missing_xref_bare_comention == 0


async def test_handler_runs_end_to_end(maker: async_sessionmaker) -> None:
    # The worker dispatch entry (factory + payload-only closure) runs the linter against a real
    # corpus without error — the shape the seeded trigger fires.
    from jbrain.wiki.lint import wiki_lint_handler

    e = await _entity(maker, "general", "E")
    for i in range(3):
        await _fact(maker, e, "general", f"claim {i}")
    handler = wiki_lint_handler(maker, embedding_model="fake-embed")
    await handler({})  # no exception; a coverage gap is reported (E has no article), nothing raised
