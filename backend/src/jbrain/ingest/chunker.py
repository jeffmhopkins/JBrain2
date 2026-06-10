"""Pure chunking functions: source text -> paragraph and section chunks.

Two granularities feed retrieval (paragraph = precise citations, section =
context for the LLM). Offsets are Python string indices into the exact source
text, so `chunk.text == source[char_start:char_end]` always holds — Step 3
embeds chunk text and citations rely on these spans.
"""

import re
from dataclasses import dataclass

PARAGRAPH = "paragraph"
SECTION = "section"

# Soft size targets, in characters. Paragraphs merge forward to MIN and
# hard-split past MAX on sentence boundaries; sections target the MIN..MAX
# band via heading groups or fixed windows.
PARAGRAPH_MIN = 200
PARAGRAPH_MAX = 1200
SECTION_MIN = 800
SECTION_MAX = 1600

_BLANK_LINE = re.compile(r"\n[ \t\r]*\n\s*")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?…])[\"')\]]*\s+")
_HEADING = re.compile(r"^#{1,6}\s")


@dataclass(frozen=True)
class TextChunk:
    granularity: str
    text: str
    char_start: int
    char_end: int


def _trimmed(source: str, start: int, end: int) -> tuple[int, int]:
    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    return start, end


def _paragraph_spans(source: str) -> list[tuple[int, int]]:
    """Blank-line-separated spans, trimmed; empty ones dropped."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for match in _BLANK_LINE.finditer(source):
        spans.append((pos, match.start()))
        pos = match.end()
    spans.append((pos, len(source)))
    trimmed = (_trimmed(source, s, e) for s, e in spans)
    return [(s, e) for s, e in trimmed if e > s]


def _split_long_span(source: str, start: int, end: int, max_len: int) -> list[tuple[int, int]]:
    """Split [start, end) at sentence boundaries; raw cuts only as last resort."""
    breaks = [start + m.end() for m in _SENTENCE_BREAK.finditer(source[start:end])]
    pieces: list[tuple[int, int]] = []
    cur = start
    while cur < end:
        if end - cur <= max_len:
            pieces.append((cur, end))
            break
        candidates = [b for b in breaks if cur < b <= cur + max_len]
        cut = max(candidates) if candidates else cur + max_len
        pieces.append((cur, cut))
        cur = cut
    trimmed = (_trimmed(source, s, e) for s, e in pieces)
    return [(s, e) for s, e in trimmed if e > s]


def paragraph_chunks(source: str) -> list[TextChunk]:
    """Paragraph-granularity chunks: merge tiny ones forward, split huge ones."""
    spans = _paragraph_spans(source)
    merged: list[tuple[int, int]] = []
    open_start: int | None = None
    for start, end in spans:
        if open_start is None:
            open_start = start
        if end - open_start >= PARAGRAPH_MIN:
            merged.append((open_start, end))
            open_start = None
    if open_start is not None:
        merged.append((open_start, spans[-1][1]))

    final: list[tuple[int, int]] = []
    for start, end in merged:
        if end - start > PARAGRAPH_MAX:
            final.extend(_split_long_span(source, start, end, PARAGRAPH_MAX))
        else:
            final.append(_trimmed(source, start, end))
    return [TextChunk(PARAGRAPH, source[s:e], s, e) for s, e in final if e > s]


def _windows(units: list[TextChunk], max_len: int) -> list[tuple[int, int]]:
    """Greedy unit windows up to max_len with one-unit overlap between them."""
    windows: list[tuple[int, int]] = []
    i = 0
    while i < len(units):
        j = i + 1
        while j < len(units) and units[j].char_end - units[i].char_start <= max_len:
            j += 1
        windows.append((i, j))
        if j >= len(units):
            break
        # Re-using the last unit gives retrieval continuity across the cut;
        # a single-unit window can't overlap without looping forever.
        i = j - 1 if j - i > 1 else j
    return windows


def _group_spans(source: str, groups: list[list[TextChunk]]) -> list[TextChunk]:
    chunks = []
    for group in groups:
        start, end = group[0].char_start, group[-1].char_end
        chunks.append(TextChunk(SECTION, source[start:end], start, end))
    return chunks


def section_chunks(source: str, units: list[TextChunk] | None = None) -> list[TextChunk]:
    """Section-granularity chunks built over paragraph chunks.

    Markdown headings define section starts when present; otherwise fixed
    windows with one-paragraph overlap cover unstructured text. Undersized
    heading sections merge forward; oversized ones re-split into windows.
    """
    units = paragraph_chunks(source) if units is None else units
    if not units:
        return []

    if not any(_HEADING.match(u.text) for u in units):
        return _group_spans(source, [units[i:j] for i, j in _windows(units, SECTION_MAX)])

    groups: list[list[TextChunk]] = []
    for unit in units:
        if not groups or (_HEADING.match(unit.text) and groups[-1]):
            groups.append([unit])
        else:
            groups[-1].append(unit)

    sized: list[list[TextChunk]] = []
    buffer: list[TextChunk] = []
    for group in groups:
        buffer.extend(group)
        if buffer[-1].char_end - buffer[0].char_start >= SECTION_MIN:
            sized.append(buffer)
            buffer = []
    if buffer:
        sized.append(buffer)  # a small tail section beats losing heading alignment

    final: list[list[TextChunk]] = []
    for group in sized:
        if group[-1].char_end - group[0].char_start > SECTION_MAX:
            final.extend(group[i:j] for i, j in _windows(group, SECTION_MAX))
        else:
            final.append(group)
    return _group_spans(source, final)


def chunk_text(source: str) -> list[TextChunk]:
    """Paragraph + section chunks for one source text.

    A short single-paragraph source yields exactly one paragraph chunk —
    its section twin would be byte-identical, pure index noise.
    """
    paragraphs = paragraph_chunks(source)
    if len(paragraphs) <= 1:
        return paragraphs
    sections = section_chunks(source, paragraphs)
    return paragraphs + sections
