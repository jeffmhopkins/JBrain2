"""Per-source parser selection + word-geometry extraction
(docs/plans/EMR_IMPORT_PLAN.md §6.2/§6.3): each fixture fingerprints to its own
parser, an unrecognized file fails closed, and a real PDF round-trips its word
boxes for the geometry parser.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from jbrain.ingest.emr.dispatch import (
    Attachment,
    Source,
    parse_corpus,
    parse_source,
    select_source,
)
from jbrain.ingest.emr.onecontent import pdf_word_pages

_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "emr"


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("epic_report.txt", Source.EPIC),
        ("athena_panel.txt", Source.ATHENA),
        ("aria_ocr.txt", Source.ARIA),
        ("onecontent_account.txt", Source.ONECONTENT),
    ],
)
def test_each_fixture_selects_its_source(fixture: str, expected: Source) -> None:
    assert select_source((_DIR / fixture).read_text()) is expected


def test_unrecognized_file_fails_closed() -> None:
    # No confident fingerprint -> None, so the caller routes the file to review
    # rather than free-extracting it (§6.3).
    assert select_source("just a grocery list: milk, eggs, bread") is None


def test_parse_source_dispatches_text_parsers() -> None:
    epic = parse_source(Source.EPIC, (_DIR / "epic_report.txt").read_text())
    assert epic.encounters  # the Epic parser ran
    athena = parse_source(Source.ATHENA, (_DIR / "athena_panel.txt").read_text())
    assert athena.encounters and athena.encounters[0].source_system == "athena"
    aria = parse_source(Source.ARIA, (_DIR / "aria_ocr.txt").read_text())
    assert aria.orphan_observations and not aria.encounters


def test_parse_source_onecontent_uses_word_geometry() -> None:
    # A synthetic PDF laid out as a OneContent table; the geometry parser recovers it
    # from real get_text("words") boxes (no char-offset ruler).
    doc = pymupdf.open()
    page = doc.new_page(width=800, height=1000)  # wide enough for the collected column
    page.insert_text((40, 60), "Account: C0000202101")
    header_y = 90.0
    for label, x in (
        ("Analyte", 40),
        ("Result", 240),
        ("Ab", 292),
        ("Units", 332),
        ("Ref", 412),
        ("Collected", 520),
    ):
        page.insert_text((x, header_y), label)
    row_y = 110.0
    for word, x in (
        ("Platelet", 40),
        ("count", 100),
        ("205", 240),
        ("10*3/uL", 332),
        ("150-400", 412),
        ("11/03/2020", 520),
        ("10:48", 590),
    ):
        page.insert_text((x, row_y), word)
    data = doc.tobytes()
    doc.close()

    result = parse_source(
        Source.ONECONTENT, "Account: C0000202101", word_pages=pdf_word_pages(data)
    )
    encs = result.encounters
    assert len(encs) == 1 and encs[0].key == "C0000202101"
    obs = encs[0].observations
    assert len(obs) == 1
    assert obs[0].analyte.name == "Platelet count"  # multi-word analyte joined by geometry
    assert obs[0].value_num == 205.0


def test_parse_corpus_reconciles_across_sources() -> None:
    # An Epic + athena + ARIA corpus: the precise sources parse for integration; the
    # ARIA reprint reads reconcile — corroborating a same-day precise draw or parking.
    corpus = parse_corpus(
        [
            Attachment(text=(_DIR / "epic_report.txt").read_text(), ref="epic.pdf"),
            Attachment(text=(_DIR / "athena_panel.txt").read_text(), ref="athena.pdf"),
            Attachment(text=(_DIR / "aria_ocr.txt").read_text(), ref="aria.pdf"),
            Attachment(text="a random unrelated document", ref="junk.pdf"),
        ]
    )
    sources = {p.source for p in corpus.precise}
    assert sources == {Source.EPIC, Source.ATHENA}  # ARIA is not integrated directly
    assert corpus.unrecognized == ["junk.pdf"]  # fail-closed -> routed to review
    # The ARIA reads reconciled against the precise Epic 2021 draws: some corroborate,
    # the readable-but-unmatched 07/29 potassium parks.
    rec = corpus.reconciliation
    assert rec.corroborated or rec.parked
    assert any(p.observation.analyte.name == "Potassium" for p in rec.parked)
