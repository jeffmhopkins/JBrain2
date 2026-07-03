"""The athena outpatient-panel parser (docs/plans/EMR_IMPORT_PLAN.md §6.3):
label→value blocks into one ambulatory encounter with an ordering provider; a
cancelled result suppresses its value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jbrain.ingest.emr.athena import fingerprint, parse_athena

_TEXT = (Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "athena_panel.txt").read_text()
_RESULT = parse_athena(_TEXT)


def test_fingerprint_matches_athena() -> None:
    assert fingerprint(_TEXT)
    assert not fingerprint("Account: C123 (this is OneContent, not athena)")


def test_one_ambulatory_encounter_with_ordering_provider() -> None:
    assert len(_RESULT.encounters) == 1
    enc = _RESULT.encounters[0]
    assert enc.encounter_class == "ambulatory"
    assert enc.source_system == "athena"
    assert enc.admitted_at == datetime(2024, 8, 12, tzinfo=UTC)  # the visit date
    roles = {(p.name, p.role) for p in enc.providers}
    assert roles == {("Ortiz, Miguel MD", "ordering")}


def test_final_results_parse_with_value_and_specimen() -> None:
    obs = {o.analyte.name: o for o in _RESULT.encounters[0].observations}
    plt = obs["Platelet count"]
    assert plt.value_num == 188.0
    assert plt.unit == "10*3/uL" and plt.ref_low == 150.0 and plt.ref_high == 400.0
    assert plt.collected_at == datetime(2024, 8, 12, 11, 20, tzinfo=UTC)
    assert plt.specimen_id == "A2024-88120"  # the accession is the §3.3 specimen
    assert plt.fhir_status == "final"
    pot = obs["Potassium"]
    assert pot.value_num == 4.6 and pot.specimen_id == "A2024-88120"


_FLAGGED = """--- page 1 ---
athenahealth Patient Portal — Lab Results (SYNTHETIC)
Ordering Provider: Ng, Priya MD
Encounter: Ambulatory visit 03/04/2024

Specimen/Accession ID: A2024-1
  Collected: 03/04/2024 09:00
  Analyte: Hemoglobin
  Result: 18.9
  Units: g/dL
  Reference Range: 13.5-17.5
  Flag: H
  Status: final
"""


def test_flag_drives_interpretation() -> None:
    enc = parse_athena(_FLAGGED).encounters[0]
    hgb = enc.observations[0]
    assert hgb.interpretation == "high"  # the "H" flag
    assert hgb.value_num == 18.9


def test_cancelled_result_suppresses_value() -> None:
    creat = next(o for o in _RESULT.encounters[0].observations if o.analyte.name == "Creatinine")
    assert creat.fhir_status == "cancelled"
    assert creat.value_num is None  # the hemolyzed specimen's value is suppressed
    assert creat.specimen_id == "A2024-88121"  # a different draw than the CBC accession
    # It is still a dated, cited record of a cancelled test, not silently dropped.
    assert creat.collected_at == datetime(2024, 8, 12, 11, 20, tzinfo=UTC)
