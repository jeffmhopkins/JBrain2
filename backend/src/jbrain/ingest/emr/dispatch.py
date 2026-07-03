"""Per-source parser selection (docs/plans/EMR_IMPORT_PLAN.md §6.3).

A decrypted attachment is routed to exactly one parser by a **fingerprint** on its
extracted text — fail-closed: no confident match returns None so the caller routes
the whole file to review rather than free-extracting it. Fingerprints are tried
most-distinctive first so a source that could echo another's markers (an ARIA OCR
reprint of OneContent lab tables) is caught by its own banner before the generic
`Account:`/legend check.

OneContent alone needs word geometry (§6.2): its `parse` consumes the
`get_text("words")` pages, while Epic/athena/ARIA parse the reading-order text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from jbrain.ingest.emr import aria, athena, epic, onecontent
from jbrain.ingest.emr.candidates import CandidateObservation, ParseResult
from jbrain.ingest.emr.onecontent import WordBox
from jbrain.ingest.emr.reconcile import Reconciliation, reconcile


class Source(Enum):
    EPIC = "epic"
    ATHENA = "athena"
    ARIA = "aria"
    ONECONTENT = "onecontent"


# Most-distinctive fingerprint first: athena's block key and Epic's date header are
# unambiguous; ARIA's banner is checked before OneContent's generic `Account:`/legend
# so an OCR reprint of the OneContent tables routes to the OCR parser, not the text one.
_FINGERPRINTS: tuple[tuple[Source, Callable[[str], bool]], ...] = (
    (Source.ATHENA, athena.fingerprint),
    (Source.EPIC, epic.fingerprint),
    (Source.ARIA, aria.fingerprint),
    (Source.ONECONTENT, onecontent.fingerprint),
)


def select_source(text: str) -> Source | None:
    """The source whose fingerprint matches, most-distinctive first; None when none
    match (the file routes to review — never free-extracted, §6.3)."""
    for source, fingerprint in _FINGERPRINTS:
        if fingerprint(text):
            return source
    return None


def parse_source(
    source: Source, text: str, *, word_pages: list[list[WordBox]] | None = None
) -> ParseResult:
    """Parse a decrypted attachment with the selected source's parser. `word_pages`
    (the `get_text("words")` geometry) is required for OneContent and ignored by the
    text-layer/OCR parsers, which read `text`."""
    if source is Source.EPIC:
        return epic.parse_epic(text)
    if source is Source.ATHENA:
        return athena.parse_athena(text)
    if source is Source.ARIA:
        return aria.parse_aria(text)
    return onecontent.parse_onecontent(word_pages or [], legend_text=text)


# The OCR source whose reads are reconciled against the precise sources rather than
# integrated directly (§6.4) — a reprint corroborates or parks, never a rival draw.
_OCR_SOURCES = frozenset({Source.ARIA})


@dataclass(frozen=True)
class Attachment:
    """One decrypted attachment's inputs: its extracted text and, for a geometry
    source, its `get_text("words")` pages. `ref` is an opaque caller handle (an
    attachment id / index) echoed back so the caller can anchor the parse."""

    text: str
    ref: str
    word_pages: list[list[WordBox]] | None = None


@dataclass(frozen=True)
class ParsedSource:
    source: Source
    ref: str
    result: ParseResult


@dataclass(frozen=True)
class Corpus:
    """The result of parsing a whole decrypted corpus: the precise parses to
    integrate (keyed by their attachment ref), the cross-source reconciliation
    (corroborations + parked OCR reads), and the refs that matched no parser
    (fail-closed, routed to review — never free-extracted, §6.3)."""

    precise: list[ParsedSource]
    reconciliation: Reconciliation
    unrecognized: list[str] = field(default_factory=list)


def _all_observations(result: ParseResult) -> list[CandidateObservation]:
    return [o for e in result.encounters for o in e.observations] + result.orphan_observations


def parse_corpus(attachments: list[Attachment]) -> Corpus:
    """Parse a decrypted corpus end-to-end (§6.3/§6.4), no I/O: dispatch each
    attachment to its parser, then reconcile the OCR reprints against the precise
    draws. The precise parses integrate as-is; the OCR source is never integrated
    directly — its reads corroborate a precise draw or park in review."""
    precise: list[ParsedSource] = []
    ocr_reads: list[CandidateObservation] = []
    precise_reads: list[CandidateObservation] = []
    unrecognized: list[str] = []
    for att in attachments:
        source = select_source(att.text)
        if source is None:
            unrecognized.append(att.ref)
            continue
        result = parse_source(source, att.text, word_pages=att.word_pages)
        if source in _OCR_SOURCES:
            ocr_reads.extend(_all_observations(result))
        else:
            precise.append(ParsedSource(source=source, ref=att.ref, result=result))
            precise_reads.extend(_all_observations(result))
    return Corpus(
        precise=precise,
        reconciliation=reconcile(precise_reads, ocr_reads),
        unrecognized=unrecognized,
    )
