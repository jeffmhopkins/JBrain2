"""Which chunks survive a re-ingest, and where the doomed ones send their references.

The property under test is the one two ON DELETE CASCADEs make load-bearing: every
`entity_mentions` row and every published `wiki_citations` row of a note is destroyed by
a plain destroy-and-rebuild (COLD_REVIEW_FINDINGS section E item 6), and D6 makes that
fire on every answered clarification. So an appended block must leave the note's chunks
either identical or covered.
"""

from uuid import uuid4

from jbrain.ingest.carryover import ChunkShape, plan_carry_over
from jbrain.ingest.chunker import chunk_text


def shape(text: str, start: int, end: int, granularity: str = "paragraph") -> ChunkShape:
    return ChunkShape(
        attachment_id=None,
        source_kind="note",
        source_anchor=None,
        granularity=granularity,
        domain_code="general",
        char_start=start,
        char_end=end,
        text=text,
    )


def shapes_for(body: str) -> list[ChunkShape]:
    """The shapes ingest would build for one note body — the real chunker, so the
    merge-forward and re-window behaviour under test is the shipped one."""
    return [shape(c.text, c.char_start, c.char_end, c.granularity) for c in chunk_text(body)]


def test_an_unchanged_note_reuses_every_chunk() -> None:
    old = shapes_for("Ran 10k this morning.\n\nFelt fine after.")
    plan = plan_carry_over(old, list(old))
    assert [o for o, _ in plan.reuse] == list(range(len(old)))
    assert plan.reanchor == ()


def test_a_short_note_re_anchors_onto_the_chunk_that_absorbed_it() -> None:
    """The common D6 shape. A short body is one paragraph chunk; the appended block is
    under PARAGRAPH_MIN, so the chunker merges the two into ONE chunk with different
    text. Nothing is identical — but the new chunk covers the old span, so the mentions
    and citations move onto it instead of cascading away."""
    body = "Sarah moved to Golden."
    old = shapes_for(body)
    new = shapes_for(body + "\n\n[clarification 2026-09-09 14:03 UTC]\nQ: Which?\nA: Colorado.")
    plan = plan_carry_over(old, new)
    assert plan.reuse == ()
    assert plan.reanchor == ((0, 0),)
    assert new[0].text.startswith(body)


def test_a_long_note_keeps_its_body_chunks_byte_identical() -> None:
    body = "\n\n".join(f"Paragraph {i} " + "filler words here. " * 20 for i in range(4))
    old = shapes_for(body)
    new = shapes_for(body + "\n\n[clarification 2026-09-09 14:03 UTC]\nQ: Which?\nA: Colorado.")
    reused_old = plan_carry_over(old, new).reused_old
    # Every paragraph of the body comes back as the same row; only the tail moves.
    paragraphs = [i for i, s in enumerate(old) if s.granularity == "paragraph"]
    assert set(paragraphs) <= reused_old


def test_a_rewritten_body_carries_nothing_over() -> None:
    """The honest boundary: when the owner actually replaces the text there is no chunk
    to hand a span to, and the references go with it exactly as before."""
    old = shapes_for("Sarah moved to Golden.")
    plan = plan_carry_over(old, shapes_for("Dinner with Tom at the pier."))
    assert plan.reuse == ()
    assert plan.reanchor == ()


def test_a_domain_move_carries_nothing_over() -> None:
    """`wiki_citations` carries a trigger requiring citation.domain = chunk.domain, so a
    citation repointed onto a re-domained chunk would abort the whole ingest. Domain is
    part of the source key precisely so that pair never arises."""
    old = [shape("Sarah moved to Golden.", 0, 22)]
    moved = ChunkShape(
        attachment_id=None,
        source_kind="note",
        source_anchor=None,
        granularity="paragraph",
        domain_code="health",
        char_start=0,
        char_end=22,
        text="Sarah moved to Golden.",
    )
    plan = plan_carry_over(old, [moved])
    assert plan.reuse == ()
    assert plan.reanchor == ()


def test_identical_chunks_are_matched_one_for_one() -> None:
    """A note can hold two rows with the same text and span (a repeated attachment
    segment). A set-based match would fold them and strand one row's references."""
    old = [shape("same", 0, 4), shape("same", 0, 4)]
    plan = plan_carry_over(old, [shape("same", 0, 4)])
    assert plan.reuse == ((0, 0),)
    # The surplus old row is doomed, and re-anchors onto the survivor rather than
    # cascading its mentions away.
    assert plan.reanchor == ((1, 0),)


def test_the_tightest_cover_wins() -> None:
    """Two surviving chunks both cover an old span; the tighter one is where a fresh
    extraction would have anchored the surface."""
    source = "x" * 10 + "b" + "y" * 29
    old = [shape(source[10:11], 10, 11)]
    new = [shape(source[0:40], 0, 40), shape(source[8:20], 8, 20)]
    assert plan_carry_over(old, new).reanchor == ((0, 1),)


def test_a_cover_that_does_not_hold_the_same_text_is_refused() -> None:
    """Offsets alone would let a REWRITTEN body absorb the old span and put a mention's
    marks on words nobody wrote there."""
    old = [shape("Sarah moved to Golden.", 0, 22)]
    new = [shape("Dinner with Tom at the pier.", 0, 28)]
    assert plan_carry_over(old, new).reanchor == ()


def test_a_different_source_is_never_a_cover() -> None:
    """Offsets are per-source: an attachment segment's char_start indexes its own text,
    so an overlapping range in the note body means nothing."""
    att = uuid4()
    old = [
        ChunkShape(
            attachment_id=att,
            source_kind="ocr",
            source_anchor="scan.png",
            granularity="paragraph",
            domain_code="general",
            char_start=0,
            char_end=10,
            text="from a scan",
        )
    ]
    assert plan_carry_over(old, [shape("note text here", 0, 100)]).reanchor == ()
