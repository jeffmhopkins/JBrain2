"""The ARIA post-OCR parser (docs/plans/EMR_IMPORT_PLAN.md §6.2/§6.3): line-oriented
extraction of a portal reprint — date-only draws, dot-leader + OCR noise tolerated,
orphan (encounter-less) reads, and an OCR-garbled reference range kept verbatim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jbrain.ingest.emr import aria
from jbrain.ingest.emr.aria import parse_aria

_TEXT = (Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "aria_ocr.txt").read_text()
_RESULT = parse_aria(_TEXT)


def test_fingerprint_matches_aria() -> None:
    assert aria.fingerprint(_TEXT)
    assert not aria.fingerprint("Account: C123 — a OneContent cumulative report")


def test_reads_are_orphan_and_ocr_fidelity() -> None:
    # ARIA prints no ordering provider -> encounter-less orphan reads (§3.4 note).
    assert _RESULT.encounters == []
    obs = _RESULT.orphan_observations
    assert obs and all(o.source_system == "aria" for o in obs)
    assert all(o.fidelity == aria.FIDELITY_OCR for o in obs)  # loses the §6.4 tie-break
    assert all(o.specimen_id == "" for o in obs)


def test_date_only_precision_and_dot_leaders_stripped() -> None:
    by = {o.analyte.name: o for o in _RESULT.orphan_observations}
    plt = by["Platelet count"]  # printed with " . . . . ." dot leaders
    assert plt.value_num == 38.0
    assert plt.precision == "day"  # date-only OCR timestamp, no fabricated time
    assert plt.collected_at == datetime(2021, 7, 22, tzinfo=UTC)
    assert plt.interpretation == "low"  # the "L" flag
    assert plt.unit == "10*3/uL" and plt.ref_low == 150.0 and plt.ref_high == 400.0
    assert by["White blood cell count"].value_num == 3.2


def test_ocr_garbled_reference_range_kept_verbatim() -> None:
    # "(4.O-11.0)" — an O-for-0 OCR error. The range must NOT be guess-corrected; the
    # value still parses, the range is preserved as text (never a fabricated bound).
    wbc = next(o for o in _RESULT.orphan_observations if o.analyte.name == "White blood cell count")
    assert wbc.ref_low is None and wbc.ref_high is None
    assert wbc.ref_text == "4.O-11.0"


def test_second_page_collected_date_applies() -> None:
    # The page-2 "Collected 07/29/2021" — the readable-but-unmatched draw the §6.4
    # reconciler will park. The parser dates it faithfully; parking is the next stage.
    pot = next(o for o in _RESULT.orphan_observations if o.analyte.name == "Potassium")
    assert pot.collected_at == datetime(2021, 7, 29, tzinfo=UTC)
    assert pot.value_num == 4.8
