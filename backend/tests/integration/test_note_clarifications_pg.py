"""D6/D7 clarification blocks against real Postgres.

The guarantee under test is not "the owner may never edit their note" — owner editing
is shipped behaviour. It is the pair: a clarification never rewrites the body, and an
owner body edit never destroys clarifications. Then D7: the composed text is what the
chunker sees, so a block is a chunk of the same note and a fact drawn from it has a
real chunk to cite.

The sharp one is the re-ingest round trip. `test_reanalysis_pg.py` already asserts that
a re-ingest deletes every chunk of a note and nulls `facts.chunk_id`; an appended
clarification IS a re-ingest, so this file proves that W1's citation re-anchor actually
covers the path D6 makes fire on every answered question.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.analysis.purge import purge_note_artifacts
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.notes.repo import SqlNotesRepo
from jbrain.notes.service import ClarificationsAltered, NoteUpdate
from jbrain.storage import FsBlobStore
from jbrain.wiki.builder import StubRewriter, WikiBuilder
from tests.conftest import docker_available
from tests.integration.test_reanalysis_pg import (
    _NoEmbed,
    analyze,
    analyzed_note,
    extraction,
    fresh_person,
    home_fact,
)
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

BODY = "Ran 10k this morning.\n\nFelt fine after."


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def make_note(maker: async_sessionmaker[AsyncSession], body: str = BODY) -> str:
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"clar-{uuid.uuid4()}", domain="general", destination=None, body=body
    )
    return note.id


async def stored_body(maker: async_sessionmaker[AsyncSession], note_id: str) -> str:
    """The `app.notes.body` COLUMN — the author's text, never the composed view."""
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(text("SELECT body FROM app.notes WHERE id = :n"), {"n": note_id})
        ).scalar_one()


async def block_count(maker: async_sessionmaker[AsyncSession], note_id: str) -> int:
    async with scoped_session(maker, OWNER) as s:
        return int(
            (
                await s.execute(
                    text("SELECT count(*) FROM app.note_clarifications WHERE note_id = :n"),
                    {"n": note_id},
                )
            ).scalar_one()
        )


async def test_the_body_stays_frozen_and_the_block_composes_onto_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlNotesRepo(maker)
    note_id = await make_note(maker)
    composed = await repo.append_clarification(
        OWNER, note_id, question="Which 10k?", answer="The canal loop."
    )
    assert composed is not None
    assert await stored_body(maker, note_id) == BODY
    assert composed.body.startswith(BODY)
    assert "Q: Which 10k?\nA: The canal loop." in composed.body
    assert "[clarification " in composed.body
    # And the same text comes back through the ordinary read path the note view uses.
    fetched = await repo.get_note(OWNER, note_id)
    assert fetched is not None and fetched.body == composed.body


async def test_blocks_compose_in_seq_order(maker: async_sessionmaker[AsyncSession]) -> None:
    repo = SqlNotesRepo(maker)
    note_id = await make_note(maker)
    await repo.append_clarification(OWNER, note_id, question="Which 10k?", answer="Canal loop.")
    await repo.append_clarification(OWNER, note_id, question="Alone?", answer="With Dana.")
    note = await repo.get_note(OWNER, note_id)
    assert note is not None
    assert note.body.index("Canal loop.") < note.body.index("With Dana.")


async def test_an_unclarified_note_reads_back_byte_identical(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """What keeps every existing test and every already-anchored citation honest."""
    note_id = await make_note(maker)
    note = await SqlNotesRepo(maker).get_note(OWNER, note_id)
    assert note is not None and note.body == BODY


async def test_an_owner_body_edit_leaves_the_clarifications_intact(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The other half of the freeze. The editor loads the COMPOSED body, so this is
    what a real PATCH carries: the owner's new first line plus the untouched blocks."""
    repo = SqlNotesRepo(maker)
    note_id = await make_note(maker)
    composed = await repo.append_clarification(
        OWNER, note_id, question="Which 10k?", answer="The canal loop."
    )
    assert composed is not None

    edited = composed.body.replace("Ran 10k this morning.", "Ran 10k before breakfast.")
    updated = await repo.update_note(OWNER, note_id, NoteUpdate(body=edited))

    assert updated is not None
    assert await stored_body(maker, note_id) == "Ran 10k before breakfast.\n\nFelt fine after."
    assert await block_count(maker, note_id) == 1
    assert updated.body.startswith("Ran 10k before breakfast.")
    assert "A: The canal loop." in updated.body
    # Exactly one copy: the strip is what stops the blocks being baked into the column
    # and then doubled by the next compose.
    assert updated.body.count("A: The canal loop.") == 1


async def test_an_untouched_edit_round_trip_changes_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlNotesRepo(maker)
    note_id = await make_note(maker)
    composed = await repo.append_clarification(
        OWNER, note_id, question="Which 10k?", answer="The canal loop."
    )
    assert composed is not None
    updated = await repo.update_note(OWNER, note_id, NoteUpdate(body=composed.body))
    assert updated is not None and updated.body == composed.body
    assert await stored_body(maker, note_id) == BODY


async def test_a_body_that_contains_the_marker_survives_an_untouched_save(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The truncation bug, end to end through the repo. A body can legitimately contain
    `\n\n[clarification ` — the owner pastes a clarified note's displayed text into a
    new note — and once that note gets its FIRST clarification, cutting on the literal
    deletes every paragraph after it on a save that changed nothing. Silent, permanent,
    and armed forever after."""
    repo = SqlNotesRepo(maker)
    pasted = (
        "Shopping list.\n\n[clarification 2026-09-08 09:00 UTC]\nQ: which shop?\n"
        "A: the co-op.\n\nAlso milk.\n\nAnd bread."
    )
    note_id = await make_note(maker, body=pasted)
    composed = await repo.append_clarification(
        OWNER, note_id, question="Anything else?", answer="Eggs."
    )
    assert composed is not None

    updated = await repo.update_note(OWNER, note_id, NoteUpdate(body=composed.body))

    assert updated is not None
    assert await stored_body(maker, note_id) == pasted
    assert "Also milk." in updated.body and "And bread." in updated.body
    assert updated.body.count("A: Eggs.") == 1


async def test_a_patch_that_mangles_the_blocks_is_refused_and_writes_nothing(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The other side of the exact-suffix rule. Storing the string would double the
    blocks on the next compose and cutting at the marker is the bug above, so the write
    is refused whole — the note is left exactly as it was."""
    repo = SqlNotesRepo(maker)
    note_id = await make_note(maker)
    composed = await repo.append_clarification(
        OWNER, note_id, question="Which 10k?", answer="The canal loop."
    )
    assert composed is not None
    mangled = composed.body.replace("The canal loop.", "the canal loop i think")

    with pytest.raises(ClarificationsAltered):
        await repo.update_note(OWNER, note_id, NoteUpdate(body=mangled))

    assert await stored_body(maker, note_id) == BODY
    unchanged = await repo.get_note(OWNER, note_id)
    assert unchanged is not None and unchanged.body == composed.body


async def test_append_works_under_the_narrowed_owner_session_w3_will_use(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Constraint 2: a note conversation runs owner-scoped to (note_domain, 'general'),
    and W3's `ask_owner` is the only caller of this method. The enqueue rides the same
    transaction and `app.jobs` is owner-only RLS, so this is the shape that has to work
    — and the one a capability token deliberately cannot reach (see below)."""
    note_id = await make_note(maker)
    narrowed = SessionContext(
        principal_id=OWNER.principal_id,
        principal_kind="owner",
        domain_scopes=("general",),
        owner_scoped=True,
    )
    composed = await SqlNotesRepo(maker).append_clarification(
        narrowed, note_id, question="Which 10k?", answer="The canal loop."
    )
    assert composed is not None
    assert "A: The canal loop." in composed.body
    async with scoped_session(maker, OWNER) as s:
        assert (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = 'ingest_note'"
                    " AND payload->>'note_id' = :n"
                ),
                {"n": note_id},
            )
        ).scalar_one() == 1


async def test_append_fails_closed_under_a_capability_token(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Not a guard anyone wrote — `app.jobs` is `is_owner()`-only (0009), so the
    in-transaction enqueue is what refuses. Recorded because it is a real consequence of
    putting the enqueue inside the append: this method is OWNER-ONLY, and a
    capability-token caller gets a raw driver error, not a clean refusal."""
    note_id = await make_note(maker)
    token = SessionContext(
        principal_id=str(uuid.uuid4()),
        principal_kind="capability",
        domain_scopes=("general",),
    )
    with pytest.raises(ProgrammingError):
        await SqlNotesRepo(maker).append_clarification(
            token, note_id, question="Which 10k?", answer="The canal loop."
        )
    assert await block_count(maker, note_id) == 0


async def test_a_domain_move_carries_the_blocks(maker: async_sessionmaker[AsyncSession]) -> None:
    """0193's policy takes no join, so a block left in the old domain would fall out
    of its own note's scope (the 0002 attachment invariant)."""
    repo = SqlNotesRepo(maker)
    note_id = await make_note(maker)
    await repo.append_clarification(OWNER, note_id, question="Which 10k?", answer="Canal loop.")
    await repo.update_note(OWNER, note_id, NoteUpdate(domain="health"))
    async with scoped_session(maker, OWNER) as s:
        domains = (
            (
                await s.execute(
                    text("SELECT domain_code FROM app.note_clarifications WHERE note_id = :n"),
                    {"n": note_id},
                )
            )
            .scalars()
            .all()
        )
    assert domains == ["health"]


async def test_the_append_queues_its_own_re_ingest(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The note's TEXT changed, so its chunks are stale and the graph with them. The
    enqueue rides the append's own transaction rather than a caller remembering."""
    repo = SqlNotesRepo(maker)
    note_id = await make_note(maker)
    await repo.append_clarification(OWNER, note_id, question="Which 10k?", answer="Canal loop.")
    async with scoped_session(maker, OWNER) as s:
        queued = int(
            (
                await s.execute(
                    text(
                        "SELECT count(*) FROM app.jobs WHERE kind = 'ingest_note'"
                        " AND payload->>'note_id' = :n AND status = 'queued'"
                    ),
                    {"n": note_id},
                )
            ).scalar_one()
        )
        state = (
            await s.execute(
                text("SELECT ingest_state FROM app.notes WHERE id = :n"), {"n": note_id}
            )
        ).scalar_one()
    assert queued == 1
    assert state == "pending"


async def test_the_block_is_chunked_as_part_of_the_note(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """D7. Without this a fact drawn from the owner's answer has no chunk to cite,
    `wiki/builder.py`'s INNER JOIN drops it, and the graph stops re-deriving from
    notes alone."""
    repo = SqlNotesRepo(maker)
    note_id = await make_note(maker)
    composed = await repo.append_clarification(
        OWNER, note_id, question="Which 10k?", answer="The canal loop by the weir."
    )
    assert composed is not None
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note_id})

    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT text, char_start, char_end FROM app.chunks"
                    " WHERE note_id = :n AND granularity = 'paragraph' ORDER BY seq"
                ),
                {"n": note_id},
            )
        ).all()
    assert any("The canal loop by the weir." in r.text for r in rows)
    # The chunker's own invariant, now against the COMPOSED source: offsets into the
    # original body are unmoved because the blocks were appended after it.
    for r in rows:
        assert composed.body[r.char_start : r.char_end] == r.text


async def _fact_chunk(maker: async_sessionmaker[AsyncSession], note_id: str) -> str | None:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text("SELECT chunk_id::text FROM app.facts WHERE note_id = :n"), {"n": note_id}
            )
        ).scalar_one()


async def _published_citations(
    maker: async_sessionmaker[AsyncSession], note_id: str
) -> list[tuple[str, str]]:
    """The citations of every CURRENT revision that cites this note, resolved through the
    same INNER JOIN `wiki/builder.py:527` uses. Published state, not a recomputation:
    `builder._source` answers what a future rebuild WOULD cite, which is a different
    question and cannot see a revision that has already lost its citations."""
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT c.id::text, ch.text FROM app.wiki_citations c"
                    " JOIN app.chunks ch ON ch.id = c.chunk_id"
                    " JOIN app.wiki_sections sec ON sec.current_revision_id = c.revision_id"
                    " WHERE c.note_id = :n ORDER BY c.seq"
                ),
                {"n": note_id},
            )
        ).all()
    return [(r[0], r[1]) for r in rows]


async def _cited_article_note(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path, person: str
) -> str:
    """An analyzed note whose entity clears the notability gate, with a published,
    cited article built from it."""
    facts = [
        home_fact(person, "Golden", confidence=0.9),
        {**home_fact(person, "Golden", confidence=0.9), "predicate": "worksAt"},
        {**home_fact(person, "Golden", confidence=0.9), "predicate": "hasPet"},
    ]
    note_id = await analyzed_note(
        maker, tmp_path, "Sarah moved to Golden.", extraction(person, facts)
    )
    await WikiBuilder(
        maker, embed=_NoEmbed(), rewriter=StubRewriter(), embedding_model="fake-embed"
    ).refresh()
    return note_id


async def test_a_clarification_re_ingest_leaves_the_article_citing_the_note(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """COLD_REVIEW_FINDINGS section E item 6, which W1 did NOT fix: `wiki_citations`
    `chunk_id` is ON DELETE CASCADE, so a re-ingest deleted the citations of an already
    PUBLISHED revision outright. Re-anchoring them afterwards is impossible — the rows
    are gone — so `ingest.carryover` hands them over while both generations exist.

    D6 makes this fire on every answered question, on the most-cited notes."""
    note_id = await _cited_article_note(maker, tmp_path, fresh_person())
    before = await _published_citations(maker, note_id)
    assert before  # the article really did cite this note

    await SqlNotesRepo(maker).append_clarification(
        OWNER, note_id, question="Which Golden?", answer="Golden, Colorado."
    )
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note_id})

    after = await _published_citations(maker, note_id)
    # Same citation ROWS — not merely the same count, which a rebuild could fake — and
    # each still resolves to a chunk holding the sentence it cited.
    assert [c for c, _ in after] == [c for c, _ in before]
    for (_, old_text), (_, new_text) in zip(before, after, strict=True):
        assert old_text in new_text


async def test_a_clarification_re_ingest_leaves_the_note_s_facts_cited(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """`facts.chunk_id` is ON DELETE SET NULL, so a re-ingest used to blank it and only
    a later re-integration re-anchored it — leaving the fact uncitable in between, which
    is the whole window the owner sits in while waiting in the thread. Carry-over closes
    it: the fact never loses its chunk at all."""
    person = fresh_person()
    facts = [home_fact(person, "Golden", confidence=0.9)]
    note_id = await analyzed_note(
        maker, tmp_path, "Sarah moved to Golden.", extraction(person, facts)
    )
    before = await _fact_chunk(maker, note_id)
    assert before is not None

    await SqlNotesRepo(maker).append_clarification(
        OWNER, note_id, question="Which Golden?", answer="Golden, Colorado."
    )
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note_id})
    chunk_id = await _fact_chunk(maker, note_id)
    assert chunk_id is not None

    async with scoped_session(maker, OWNER) as s:
        chunk_text_now = (
            await s.execute(text("SELECT text FROM app.chunks WHERE id = :c"), {"c": chunk_id})
        ).scalar_one()
        entity_id = (
            await s.execute(
                text("SELECT entity_id FROM app.facts WHERE note_id = :n"), {"n": note_id}
            )
        ).scalar_one()
    assert "Sarah moved to Golden." in chunk_text_now

    await analyze(maker, note_id, extraction(person, facts))
    builder = WikiBuilder(
        maker, embed=_NoEmbed(), rewriter=StubRewriter(), embedding_model="fake-embed"
    )
    async with scoped_session(maker, OWNER) as s:
        sourced = await builder._source(s, entity_id)
    assert sourced is not None
    assert [c.statement for c in sourced.claims] == [f"{person} moved to Golden."]


async def test_a_clarification_re_ingest_keeps_the_note_s_entity_mentions(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """`entity_mentions.chunk_id` is ON DELETE CASCADE, so every mention of the note —
    `link_method='human'` ones included — was destroyed by the re-chunk. The rebuild
    sweep's spare-mention machinery exists because a review reopen moves rows BY ID
    (`purge.py`), so a clarification landing between a merge resolution and its reopen
    used to make the un-merge silently move nothing."""
    person = fresh_person()
    note_id = await analyzed_note(
        maker,
        tmp_path,
        "Sarah moved to Golden.",
        extraction(person, [home_fact(person, "Golden", confidence=0.9)]),
    )
    async with scoped_session(maker, OWNER) as s:
        row = (
            await s.execute(
                text(
                    "SELECT id::text, chunk_id::text, char_start, char_end, surface_text"
                    " FROM app.entity_mentions WHERE note_id = :n"
                ),
                {"n": note_id},
            )
        ).one()
        # A human link is the sharpest case: nothing re-derives it.
        await s.execute(
            text("UPDATE app.entity_mentions SET link_method = 'human' WHERE id = :i"),
            {"i": row[0]},
        )

    await SqlNotesRepo(maker).append_clarification(
        OWNER, note_id, question="Which Golden?", answer="Golden, Colorado."
    )
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note_id})

    async with scoped_session(maker, OWNER) as s:
        after = (
            await s.execute(
                text(
                    "SELECT m.id::text, m.link_method,"
                    " substring(ch.text FROM m.char_start + 1 FOR m.char_end - m.char_start)"
                    " FROM app.entity_mentions m JOIN app.chunks ch ON ch.id = m.chunk_id"
                    " WHERE m.note_id = :n"
                ),
                {"n": note_id},
            )
        ).all()
    assert [r[0] for r in after] == [row[0]]
    assert after[0][1] == "human"
    # The span is CHUNK-relative, so surviving is not enough: it must still mark the
    # same characters after the shift onto whatever chunk absorbed the old one.
    assert after[0][2] == row[4]


async def test_a_rebuild_keeps_the_clarifications(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """`keep_pinned=True` is the corpus rebuild sweep, whose whole premise is
    re-deriving the graph FROM the notes. A clarification is part of the note, so a
    rebuild that deleted one would break D7 corpus-wide and silently."""
    person = fresh_person()
    note_id = await analyzed_note(
        maker,
        tmp_path,
        "Sarah moved to Golden.",
        extraction(person, [home_fact(person, "Golden", confidence=0.9)]),
    )
    await SqlNotesRepo(maker).append_clarification(
        OWNER, note_id, question="Which Golden?", answer="Golden, Colorado."
    )
    async with scoped_session(maker, OWNER) as s:
        await purge_note_artifacts(s, uuid.UUID(note_id), keep_pinned=True)
    assert await block_count(maker, note_id) == 1
    async with scoped_session(maker, OWNER) as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.facts WHERE note_id = :n"), {"n": note_id}
            )
        ).scalar_one() == 0


async def test_deleting_the_note_removes_the_clarifications(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The soft delete keeps the `app.notes` row, so 0193's ON DELETE CASCADE never
    fires on it — the purge has to say so explicitly, or owner-typed text survives a
    deletion promise."""
    repo = SqlNotesRepo(maker)
    note_id = await make_note(maker)
    await repo.append_clarification(OWNER, note_id, question="Which 10k?", answer="Canal loop.")
    assert await repo.delete_note(OWNER, note_id) is True
    assert await block_count(maker, note_id) == 0


async def test_append_refuses_a_note_out_of_scope(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note_id = await make_note(maker)
    health_only = SessionContext(
        principal_id=str(uuid.uuid4()),
        principal_kind="owner",
        domain_scopes=("health",),
        owner_scoped=True,
    )
    assert (
        await SqlNotesRepo(maker).append_clarification(
            health_only, note_id, question="q", answer="a"
        )
        is None
    )
