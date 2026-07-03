"""The Epic parser (docs/plans/EMR_IMPORT_PLAN.md §6.3) against the synthetic
fixture — banner-mode inpatient/outpatient, the MICU->A3 transfer linkage,
transfusion events, analyte canonicalization, FHIR status, and the pathology
narrative kept as prose. No DB, no LLM.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jbrain.ingest.emr.candidates import canonicalize_analyte
from jbrain.ingest.emr.epic import fingerprint, parse_epic

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "epic_report.txt"
_TEXT = _FIXTURE.read_text()
_RESULT = parse_epic(_TEXT)


def _enc(pred) -> object:
    return next(e for e in _RESULT.encounters if pred(e))


def test_fingerprint_matches_epic() -> None:
    assert fingerprint(_TEXT)
    assert not fingerprint("KEY FOR ABNORMAL COLUMN\nAccount: C123")


def test_four_encounters_parsed() -> None:
    assert len(_RESULT.encounters) == 4


def test_inpatient_vs_outpatient_by_banner_mode() -> None:
    a3 = _enc(lambda e: "A3" in (e.facility or ""))
    micu = _enc(lambda e: "MICU" in (e.facility or ""))
    outpatient = [e for e in _RESULT.encounters if e.encounter_class == "ambulatory"]
    assert a3.encounter_class == "inpatient" and micu.encounter_class == "inpatient"
    assert a3.care_unit == "A3" and micu.care_unit == "MICU"
    assert len(outpatient) == 2  # the two lab visits (Visit date banners)


def test_admit_discharge_dates() -> None:
    micu = _enc(lambda e: e.care_unit == "MICU")
    a3 = _enc(lambda e: e.care_unit == "A3")
    assert micu.admitted_at == datetime(2026, 1, 25)
    assert micu.discharged_at == datetime(2026, 1, 28)
    assert a3.admitted_at == datetime(2026, 1, 28)
    assert a3.discharged_at == datetime(2026, 2, 1)


def test_micu_to_a3_transfer_links_part_of() -> None:
    micu = _enc(lambda e: e.care_unit == "MICU")
    a3 = _enc(lambda e: e.care_unit == "A3")
    # The A3 segment is part of the episode whose first encounter is the MICU stay.
    assert a3.part_of_key == micu.key
    assert micu.part_of_key is None


def test_providers_and_roles() -> None:
    micu = _enc(lambda e: e.care_unit == "MICU")
    roles = {(p.name, p.role) for p in micu.providers}
    assert ("Chen, Sarah MD", "attending") in roles
    assert ("Lee, Pat RN", "collecting_rn") in roles


def test_diagnosis_icd10() -> None:
    a3 = _enc(lambda e: e.care_unit == "A3")
    assert any(d.icd10 == "D69.6" and "Thrombocytopenia" in d.label for d in a3.diagnoses)


def test_transfusion_events() -> None:
    a3 = _enc(lambda e: e.care_unit == "A3")
    orders = {t.order_id: t for t in a3.transfusions}
    assert orders["TX7781"].product == "PLATELET" and orders["TX7781"].units == 1
    assert orders["TX7782"].product == "FFP" and orders["TX7782"].units == 2


def test_observations_values_flags_status() -> None:
    a3 = _enc(lambda e: e.care_unit == "A3")
    plt = next(o for o in a3.observations if o.analyte.name == "Platelet count")
    assert plt.value_num == 9 and plt.unit == "10*3/uL"
    assert plt.ref_low == 150 and plt.ref_high == 400
    assert plt.interpretation == "critical"  # (L*)
    assert plt.specimen_id == "H8202188-8"
    assert plt.fhir_status == "corrected"
    assert plt.collected_at == datetime(2026, 2, 1, 6, 14)


def test_analyte_canonicalization_across_labels() -> None:
    # WBC printed as "White Blood Cell Count" (MICU) and "Leukocytes" (outpatient)
    # resolve to one canonical code.
    wbc_labels = ["White Blood Cell Count", "Leukocytes", "WBC"]
    codes = {canonicalize_analyte(x).code for x in wbc_labels}
    assert codes == {"6690-2"}
    assert canonicalize_analyte("WBC").name == "White blood cell count"
    # An unmapped analyte is flagged, not given a guessed LOINC.
    unk = canonicalize_analyte("Ferritin")
    assert unk.loinc is None and not unk.mapped and unk.code.startswith("slug:")


def test_qualifier_is_collected_iso_pipe_specimen() -> None:
    a3 = _enc(lambda e: e.care_unit == "A3")
    plt = next(o for o in a3.observations if o.analyte.name == "Platelet count")
    assert plt.qualifier == "2026-02-01T06:14:00|H8202188-8"


def test_pathology_narrative_kept_as_prose() -> None:
    assert _RESULT.pathology_narrative is not None
    assert "Final Diagnosis" in _RESULT.pathology_narrative
    assert "hypocellular marrow" in _RESULT.pathology_narrative
    # The narrative is NOT shredded into observation facts.
    assert all("marrow" not in (o.analyte.name.lower()) for e in _RESULT.encounters
               for o in e.observations)


def test_potassium_no_flag_has_no_interpretation() -> None:
    micu = _enc(lambda e: e.care_unit == "MICU")
    k = next(o for o in micu.observations if o.analyte.name == "Potassium")
    assert k.value_num == 4.4 and k.interpretation is None and k.fhir_status == "final"
