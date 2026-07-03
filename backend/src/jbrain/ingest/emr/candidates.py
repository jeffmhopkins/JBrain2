"""Typed parser output for the EMR importer (docs/plans/EMR_IMPORT_PLAN.md §6.3).

Per-source parsers are pure functions of decrypted text that emit these typed
candidates — never graph writes. The `EmrImporter` (§6.6) lowers them into
`IntegrationIntent`s. Analyte labels are canonicalized to a stable code here
(§6.3) because that code, not the raw printed label, is what entity identity and
the cross-source dedup key on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# --- Analyte canonicalization (load-bearing for dedup, §6.3) --------------
#
# A small curated LOINC subset for the common CBC/CMP/coag panel, keyed by a
# normalized synonym. Unmapped analytes get loinc=None and a slug code (never a
# guessed LOINC) and are FLAGGED so they can't masquerade as dedup-eligible.


@dataclass(frozen=True)
class Analyte:
    code: str  # the canonical, stable identity key (LOINC when known, else a slug)
    name: str  # the display canonical name
    loinc: str | None
    mapped: bool  # False => resolved only to a slug; not a confident dedup key


# canonical name -> (loinc, {synonyms})
_LOINC_SUBSET: dict[tuple[str, str | None], set[str]] = {
    ("Platelet count", "777-3"): {"platelet count", "platelets", "plt"},
    ("Hemoglobin", "718-7"): {"hemoglobin", "hgb", "hb"},
    ("White blood cell count", "6690-2"): {
        "white blood cell count",
        "wbc",
        "leukocytes",
        "leucocytes",
        "white blood cells",
    },
    ("Potassium", "2823-3"): {"potassium", "k", "k+"},
    ("Creatinine", "2160-0"): {"creatinine", "cr", "creat"},
    ("Sodium", "2951-2"): {"sodium", "na", "na+"},
}

_SYNONYM_TO_CANON: dict[str, tuple[str, str]] = {
    syn: (name, loinc)
    for (name, loinc), syns in _LOINC_SUBSET.items()
    if loinc is not None
    for syn in syns
}


def _normalize_label(label: str) -> str:
    """Lowercase, strip OCR noise (dot leaders, stray punctuation) and collapse
    whitespace so `White Blood Cell Count` and `wbc . . .` normalize alike."""
    s = label.lower().strip()
    s = re.sub(r"[.•]+", " ", s)  # dot leaders / bullets
    s = re.sub(r"[^a-z0-9+ ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _normalize_label(label)).strip("_") or "unknown"


def canonicalize_analyte(label: str) -> Analyte:
    """Resolve a printed analyte label to a canonical code + LOINC. An unmapped
    label resolves to its slug with `mapped=False` (never a guessed LOINC)."""
    norm = _normalize_label(label)
    hit = _SYNONYM_TO_CANON.get(norm)
    if hit is not None:
        name, loinc = hit
        return Analyte(code=loinc, name=name, loinc=loinc, mapped=True)
    return Analyte(code=f"slug:{_slug(label)}", name=label.strip(), loinc=None, mapped=False)


# --- Candidate value objects ----------------------------------------------

# FHIR report-status vocabulary the parsers map an EMR reading's status into.
FHIR_STATUSES = frozenset(
    {"registered", "preliminary", "final", "amended", "corrected", "cancelled", "entered-in-error"}
)


@dataclass(frozen=True)
class CandidateObservation:
    """One draw of one analyte (§3.2). `fidelity` ranks text-layer > OCR for the
    dedup winner; `fhir_status` drives the §3.5 lifecycle transition."""

    analyte: Analyte
    collected_at: datetime
    value_num: float | None
    value_text: str | None
    unit: str | None
    ref_low: float | None
    ref_high: float | None
    ref_text: str | None
    interpretation: str | None
    specimen_id: str
    performing_lab: str | None
    fhir_status: str
    source_system: str
    fidelity: int  # higher = more authoritative (text-layer=2, ocr=1)
    source_anchor: str  # "page N" — the chunk this draw cites
    precision: str = "instant"  # temporal precision for the effectiveDate token

    @property
    def qualifier(self) -> str:
        """The §3.3 dedup/idempotency address: `<collected_iso>|<specimen_or_empty>`."""
        return f"{self.collected_at.isoformat()}|{self.specimen_id}"


@dataclass(frozen=True)
class CandidateProvider:
    name: str
    role: str  # attending|ordering|authorizing|pathologist|collecting_rn


@dataclass(frozen=True)
class CandidateDiagnosis:
    icd10: str
    label: str


@dataclass(frozen=True)
class CandidateTransfusion:
    order_id: str
    product: str
    units: int
    indication: str


@dataclass
class CandidateEncounter:
    """A hospitalization or lab-visit segment (§3.4) with the draws it encloses."""

    key: str  # a stable within-file grouping key (encounter header / account)
    encounter_class: str  # inpatient|emergency|ambulatory|observation
    facility: str | None
    care_unit: str | None
    admitted_at: datetime | None
    discharged_at: datetime | None
    disposition: str | None
    source_system: str
    source_anchor: str
    part_of_key: str | None = None  # a facility transfer: the episode's first encounter key
    providers: list[CandidateProvider] = field(default_factory=list)
    diagnoses: list[CandidateDiagnosis] = field(default_factory=list)
    transfusions: list[CandidateTransfusion] = field(default_factory=list)
    observations: list[CandidateObservation] = field(default_factory=list)


@dataclass
class ParseResult:
    """A parser's whole output for one decrypted attachment."""

    encounters: list[CandidateEncounter] = field(default_factory=list)
    orphan_observations: list[CandidateObservation] = field(default_factory=list)
    pathology_narrative: str | None = None  # kept as prose (§6.5), never shredded
    blood_type: dict[str, str] | None = None  # {"abo","rh"} -> "Me" (§3.7)
