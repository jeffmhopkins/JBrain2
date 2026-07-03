"""Cross-source candidate reconciliation — the dedup enforcer
(docs/plans/EMR_IMPORT_PLAN.md §6.4). The four red-team scenarios: divergent
renderings of the same draw dedup (OCR corroborates, adopting the precise
address); a readable-but-wrong OCR timestamp parks in review; two genuinely
distinct specimen-less draws both persist; and matching is by canonical LOINC,
not the printed label.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jbrain.ingest.emr.candidates import CandidateObservation, canonicalize_analyte
from jbrain.ingest.emr.reconcile import PARK_SUBKIND, reconcile


def _obs(
    label: str,
    value: float | None,
    collected: datetime,
    *,
    specimen: str = "",
    fidelity: int = 2,
    precision: str = "instant",
    source: str = "onecontent",
) -> CandidateObservation:
    return CandidateObservation(
        analyte=canonicalize_analyte(label),
        collected_at=collected,
        value_num=value,
        value_text=None,
        unit=None,
        ref_low=None,
        ref_high=None,
        ref_text=None,
        interpretation=None,
        specimen_id=specimen,
        performing_lab=None,
        fhir_status="final",
        source_system=source,
        fidelity=fidelity,
        source_anchor="page 1",
        precision=precision,
    )


def _ocr(label: str, value: float | None, day: datetime) -> CandidateObservation:
    return _obs(label, value, day, fidelity=1, precision="day", source="aria")


# The 2021 OneContent draw (precise) and its ARIA reprint (date-only OCR).
_PRECISE_2021 = datetime(2021, 7, 22, 7, 12, tzinfo=UTC)
_ARIA_DAY = datetime(2021, 7, 22, tzinfo=UTC)


def test_divergent_renderings_of_same_draw_dedup() -> None:
    precise = [_obs("Platelet count", 38, _PRECISE_2021, specimen="SP-1")]
    ocr = [_ocr("Platelet count", 38, _ARIA_DAY)]
    result = reconcile(precise, ocr)
    # The precise draw is authoritative; the OCR read corroborates, not duplicates.
    assert result.to_lower == precise
    assert result.parked == []
    assert len(result.corroborated) == 1
    c = result.corroborated[0]
    # The OCR read ADOPTED the precise address -> identical qualifier -> idempotent upsert.
    assert c.ocr.collected_at == _PRECISE_2021
    assert c.ocr.specimen_id == "SP-1"
    assert c.ocr.qualifier == c.precise.qualifier


def test_readable_but_wrong_timestamp_parks_in_review() -> None:
    # ARIA prints Potassium on 07/29; the only precise Potassium draw is 07/22 (7 days
    # out) — a readable-but-wrong date. It must PARK, never mint a spurious point.
    precise = [_obs("Potassium", 4.1, _PRECISE_2021)]
    ocr = [_ocr("Potassium", 4.8, datetime(2021, 7, 29, tzinfo=UTC))]
    result = reconcile(precise, ocr)
    assert result.corroborated == []
    assert len(result.parked) == 1
    assert result.parked[0].subkind == PARK_SUBKIND
    assert result.parked[0].observation.value_num == 4.8


def test_in_window_but_value_disagrees_parks() -> None:
    # Same day, but the OCR value disagrees beyond tolerance (a misread digit) — a
    # different/misread reading, never silently merged (§6.4).
    precise = [_obs("Platelet count", 38, _PRECISE_2021)]
    ocr = [_ocr("Platelet count", 88, _ARIA_DAY)]  # 3<->8 OCR confusion
    result = reconcile(precise, ocr)
    assert result.corroborated == []
    assert len(result.parked) == 1


def test_next_day_within_window_still_reconciles() -> None:
    # Midnight/timezone drift on a reprint: ±1 day is inside the window.
    precise = [_obs("Hemoglobin", 10.9, _PRECISE_2021)]
    ocr = [_ocr("Hemoglobin", 10.9, datetime(2021, 7, 23, tzinfo=UTC))]
    result = reconcile(precise, ocr)
    assert len(result.corroborated) == 1 and result.parked == []


def test_two_specimen_less_draws_both_persist() -> None:
    # Genuinely distinct specimen-less draws differ in collected_at -> different
    # qualifiers -> both survive (the reconciler never merges precise with precise).
    d1 = datetime(2020, 11, 3, 10, 48, tzinfo=UTC)
    d2 = datetime(2022, 5, 19, 14, 3, tzinfo=UTC)
    precise = [_obs("Platelet count", 205, d1), _obs("Platelet count", 171, d2)]
    result = reconcile(precise, [])
    assert result.to_lower == precise
    quals = {o.qualifier for o in result.to_lower}
    assert len(quals) == 2  # distinct addresses, both persist


def test_matching_is_by_canonical_code_not_label() -> None:
    # OneContent prints "White Blood Cell Count"; ARIA's OCR prints "WBC". Both
    # canonicalize to LOINC 6690-2, so they reconcile despite different labels (§6.3).
    precise = [_obs("White Blood Cell Count", 3.2, _PRECISE_2021)]
    ocr = [_ocr("WBC", 3.2, _ARIA_DAY)]
    assert precise[0].analyte.code == ocr[0].analyte.code == "6690-2"
    result = reconcile(precise, ocr)
    assert len(result.corroborated) == 1 and result.parked == []
