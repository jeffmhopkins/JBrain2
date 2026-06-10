"""Pure chunker logic: spans, merging, splitting, sections, degenerate cases."""

from jbrain.ingest.chunker import (
    PARAGRAPH,
    PARAGRAPH_MAX,
    PARAGRAPH_MIN,
    SECTION,
    SECTION_MAX,
    TextChunk,
    chunk_text,
    paragraph_chunks,
    section_chunks,
)


def assert_spans_match(source: str, chunks: list[TextChunk]) -> None:
    """The invariant Step 3 citations depend on: text is exactly its span."""
    for chunk in chunks:
        assert chunk.text == source[chunk.char_start : chunk.char_end]
        assert chunk.text == chunk.text.strip()
        assert chunk.text


def test_empty_text_yields_nothing() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  \t\n") == []


def test_one_liner_is_a_single_paragraph_chunk_without_section_twin() -> None:
    source = "Buy milk"
    chunks = chunk_text(source)
    assert len(chunks) == 1
    assert chunks[0].granularity == PARAGRAPH
    assert chunks[0].text == "Buy milk"
    assert_spans_match(source, chunks)


def test_single_paragraph_with_surrounding_whitespace_trims_spans() -> None:
    source = "\n\n  Call the dentist tomorrow.  \n\n"
    chunks = chunk_text(source)
    assert len(chunks) == 1
    assert chunks[0].text == "Call the dentist tomorrow."
    assert_spans_match(source, chunks)


def test_tiny_paragraphs_merge_forward_to_minimum() -> None:
    paras = [f"Item {i} is small." for i in range(12)]  # ~18 chars each
    source = "\n\n".join(paras)
    chunks = paragraph_chunks(source)
    assert_spans_match(source, chunks)
    # All but the trailing remainder reach the minimum.
    for chunk in chunks[:-1]:
        assert len(chunk.text) >= PARAGRAPH_MIN
    # Merged chunks keep the original blank-line structure inside the span.
    assert "\n\n" in chunks[0].text


def test_merged_trailing_remainder_may_stay_small() -> None:
    source = "x" * 250 + "\n\n" + "tail"
    chunks = paragraph_chunks(source)
    assert [c.text for c in chunks] == ["x" * 250, "tail"]
    assert_spans_match(source, chunks)


def test_huge_wall_splits_on_sentence_boundaries() -> None:
    sentence = "The quick brown fox jumps over the lazy dog near the river bank. "
    source = (sentence * 80).strip()  # ~5280 chars, no blank lines
    chunks = paragraph_chunks(source)
    assert_spans_match(source, chunks)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= PARAGRAPH_MAX
    # Every cut landed after a sentence end, not mid-sentence.
    for chunk in chunks:
        assert chunk.text.endswith("bank.")


def test_wall_without_sentence_boundaries_hard_splits() -> None:
    source = "a" * 3000
    chunks = paragraph_chunks(source)
    assert_spans_match(source, chunks)
    assert [len(c.text) for c in chunks] == [1200, 1200, 600]


def test_sections_use_fixed_windows_with_overlap_for_unstructured_text() -> None:
    paras = [f"Paragraph {i}. " + "word " * 60 for i in range(10)]  # ~315 chars each
    source = "\n\n".join(paras)
    sections = section_chunks(source)
    assert_spans_match(source, sections)
    assert len(sections) > 1
    for section in sections:
        assert len(section.text) <= SECTION_MAX
    # One-paragraph overlap: each window starts inside its predecessor.
    for prev, cur in zip(sections, sections[1:], strict=False):
        assert cur.char_start < prev.char_end


def test_markdown_headings_start_sections() -> None:
    # Big enough that each heading group clears SECTION_MIN on its own.
    body_a = "word " * 90
    body_b = "data " * 90
    source = f"# Alpha\n\n{body_a}\n\n{body_a}\n\n## Beta\n\n{body_b}\n\n{body_b}\n\n{body_b}"
    sections = section_chunks(source)
    assert_spans_match(source, sections)
    starts = [s.text.splitlines()[0] for s in sections]
    assert starts[0].startswith("# Alpha")
    assert any(s.startswith("## Beta") for s in starts)
    # Heading sections never bleed backward: Beta's body is not in Alpha's section.
    alpha = next(s for s in sections if s.text.startswith("# Alpha"))
    assert "## Beta" not in alpha.text


def test_tiny_heading_sections_merge_forward() -> None:
    source = "# A\n\nshort.\n\n# B\n\nalso short.\n\n# C\n\nstill short."
    sections = section_chunks(source)
    assert_spans_match(source, sections)
    # Three tiny headed sections collapse rather than emitting three crumbs.
    assert len(sections) == 1


def test_oversized_heading_section_resplits_into_windows() -> None:
    body = ("Sentence number one is here. " * 10).strip()  # ~290 chars per para
    source = "# Big\n\n" + "\n\n".join([body] * 8)
    sections = section_chunks(source)
    assert_spans_match(source, sections)
    assert len(sections) > 1
    for section in sections:
        assert len(section.text) <= SECTION_MAX


def test_chunk_text_emits_both_granularities_for_multi_paragraph_notes() -> None:
    paras = ["alpha " * 50, "beta " * 50, "gamma " * 50]  # ~300 chars each
    source = "\n\n".join(paras)
    chunks = chunk_text(source)
    assert_spans_match(source, chunks)
    granularities = {c.granularity for c in chunks}
    assert granularities == {PARAGRAPH, SECTION}
    # No section duplicates a paragraph chunk verbatim.
    paragraph_texts = {c.text for c in chunks if c.granularity == PARAGRAPH}
    for section in (c for c in chunks if c.granularity == SECTION):
        assert section.text not in paragraph_texts


def test_unicode_text_keeps_span_integrity() -> None:
    source = (
        "Résumé notes: naïve café owner 😀 said «привет».\n\n"
        "日本語の段落です。とても短いですが、テストには十分です。\n\n"
        + "Mixed emoji 🎉🎊 paragraph with enough characters to matter. "
        * 5
    )
    chunks = chunk_text(source)
    assert chunks
    assert_spans_match(source, chunks)


def test_crlf_style_blank_lines_split_paragraphs() -> None:
    source = "first paragraph here\r\n\r\nsecond paragraph here"
    chunks = paragraph_chunks(source)
    assert_spans_match(source, chunks)
    assert len(chunks) == 1  # tiny ones merged forward
    assert "first paragraph" in chunks[0].text
    assert "second paragraph" in chunks[0].text
