"""The athena outpatient-panel parser (docs/plans/EMR_IMPORT_PLAN.md §6.3).

athena prints text-layer `label: value` blocks, one per analyte, keyed by a
`Specimen/Accession ID`. Unlike Epic/OneContent it names an explicit
**Ordering Provider** for the panel, so the whole file is modeled as one
ambulatory `Encounter` (§3.4 note) carrying that provider via `attender[ordering]`
— the path `lab_results.orderer` projects from. Each block's `Collected` sets the
draw's `valid_from` (§3.3) and its accession is the §3.3 specimen id.

A **cancelled** result (`Status: cancelled`, e.g. a hemolyzed specimen with a
`RESULT NOTE`) carries `fhir_status="cancelled"` with its value **suppressed** —
the reading is recorded as cancelled, never as a spurious numeric measurement.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from jbrain.ingest.emr.candidates import (
    CandidateEncounter,
    CandidateObservation,
    CandidateProvider,
    ParseResult,
    canonicalize_analyte,
)

SOURCE_SYSTEM = "athena"
FIDELITY_TEXT_LAYER = 2

_ORDERING = re.compile(r"^Ordering Provider:\s*(.+)$", re.I)
_VISIT = re.compile(r"^Encounter:\s*.*?(\d{2}/\d{2}/\d{4})", re.I)
_SPECIMEN = re.compile(r"^Specimen/Accession ID:\s*(\S+)", re.I)
_FIELD = re.compile(r"^\s*([A-Za-z ]+?):\s*(.*)$")
_COLLECTED = re.compile(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})")
_REF_RANGE = re.compile(r"(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)")
# Values a cancelled reading may print; its numeric result is suppressed regardless.
_SUPPRESSED_STATUSES = frozenset({"cancelled", "entered-in-error"})


def fingerprint(text: str) -> bool:
    """A confident athena match: the per-analyte `Specimen/Accession ID` block key."""
    return bool(_SPECIMEN.search(text) or re.search(r"athenahealth", text, re.I))


def _date(day: str, hm: str) -> datetime:
    return datetime.strptime(f"{day} {hm}", "%m/%d/%Y %H:%M").replace(tzinfo=UTC)


def _interpretation(flag: str) -> str | None:
    flag = flag.strip()
    if not flag:
        return None
    if "*" in flag:
        return "critical"
    if "H" in flag.upper():
        return "high"
    if "L" in flag.upper():
        return "low"
    return "abnormal"


def _observation(block: dict[str, str], anchor: str) -> CandidateObservation | None:
    """Build one draw from a parsed `label: value` block. None when the block has no
    collected timestamp (an unusable block is skipped, never dated by guess)."""
    cm = _COLLECTED.search(block.get("collected", ""))
    if cm is None:
        return None
    status = block.get("status", "final").strip().lower() or "final"
    result = block.get("result", "").strip()
    # A cancelled/withdrawn reading suppresses its value (§6.3): recorded as cancelled,
    # never a numeric measurement even if a stale digit was printed.
    value_num: float | None = None
    if status not in _SUPPRESSED_STATUSES and result:
        try:
            value_num = float(result)
        except ValueError:
            value_num = None
    ref_low = ref_high = None
    rm = _REF_RANGE.search(block.get("reference range", ""))
    if rm:
        ref_low, ref_high = float(rm.group(1)), float(rm.group(2))
    return CandidateObservation(
        analyte=canonicalize_analyte(block.get("analyte", "").strip()),
        collected_at=_date(cm.group(1), cm.group(2)),
        value_num=value_num,
        value_text=None,
        unit=block.get("units", "").strip() or None,
        ref_low=ref_low,
        ref_high=ref_high,
        ref_text=None,
        interpretation=_interpretation(block.get("flag", "")),
        specimen_id=block.get("_accession", ""),
        performing_lab=None,
        fhir_status=status,
        source_system=SOURCE_SYSTEM,
        fidelity=FIDELITY_TEXT_LAYER,
        source_anchor=anchor,
        precision="instant",
    )


def parse_athena(text: str) -> ParseResult:
    """Parse an athena panel into one ambulatory encounter carrying its draws and the
    ordering provider. The file is a single outpatient visit (§3.4 note)."""
    result = ParseResult()
    page = "page 1"
    ordering: str | None = None
    visit: datetime | None = None
    observations: list[CandidateObservation] = []

    block: dict[str, str] = {}

    def flush() -> None:
        if block.get("_accession"):
            obs = _observation(block, page)
            if obs is not None:
                observations.append(obs)

    for raw in text.splitlines():
        line = raw.rstrip()
        pm = re.match(r"^---\s*page\s+(\d+)\s*---\s*$", line.strip(), re.I)
        if pm:
            page = f"page {pm.group(1)}"
            continue
        om = _ORDERING.match(line.strip())
        if om:
            ordering = om.group(1).strip()
            continue
        vm = _VISIT.match(line.strip())
        if vm:
            visit = _date(vm.group(1), "00:00")
            continue
        sm = _SPECIMEN.match(line.strip())
        if sm:
            flush()  # a new specimen closes the prior block
            block = {"_accession": sm.group(1)}
            continue
        fm = _FIELD.match(line)
        if fm and block:
            block[fm.group(1).strip().lower()] = fm.group(2).strip()
    flush()

    if not observations:
        return result
    providers = [CandidateProvider(name=ordering, role="ordering")] if ordering else []
    result.encounters = [
        CandidateEncounter(
            key=f"athena:{observations[0].specimen_id}",
            encounter_class="ambulatory",
            facility=None,
            care_unit=None,
            admitted_at=visit or observations[0].collected_at,
            discharged_at=visit or observations[0].collected_at,
            disposition=None,
            source_system=SOURCE_SYSTEM,
            source_anchor=page,
            providers=providers,
            observations=observations,
        )
    ]
    return result
