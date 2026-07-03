"""The Epic "EMR Report" parser (docs/plans/EMR_IMPORT_PLAN.md §2, §6.3).

A pure function of the decrypted text/segments — zero LLM, zero egress. It
survives the two structural traps the plan names: (1) banner bleed — page
banners print ABOVE the encounter header and encounters are reverse-chronological,
so a leading banner belongs to the encounter whose header follows it, and
inpatient-vs-outpatient is decided by the MODE of `Adm/DC` vs `Visit date`
banners per encounter; (2) a hospitalization can span facilities — the MICU→A3
transfer becomes two encounters linked by `part_of_key`, reconstructed by
continuity of admit/discharge dates.

Postal addresses and geo are never emitted (§3.6 Layer 1) — only facility and
provider NAMES ride a candidate. The bone-marrow pathology report is kept as
prose (§6.5), not shredded into facts.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from jbrain.ingest.emr.candidates import (
    CandidateDiagnosis,
    CandidateEncounter,
    CandidateObservation,
    CandidateProvider,
    CandidateTransfusion,
    ParseResult,
    canonicalize_analyte,
)

SOURCE_SYSTEM = "epic"
FIDELITY_TEXT_LAYER = 2

_PAGE = re.compile(r"^---\s*page\s+(\d+)\s*---\s*$", re.I)
_HEADER = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+-\s+(.+?)\s+in\s+(.+)$")
_BANNER_ADMDC = re.compile(
    r"Adm:\s*(\d{2}/\d{2}/\d{4})\s+DC:\s*(\d{2}/\d{2}/\d{4})(?:\s+Unit:\s*(\S+))?", re.I
)
_BANNER_VISIT = re.compile(r"Visit date:\s*(\d{2}/\d{2}/\d{4})", re.I)
_COLLECTED = re.compile(r"collected\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})", re.I)
_OBS = re.compile(
    r"^\s*(?P<analyte>.+?)\s+(?P<value>-?\d+(?:\.\d+)?)\s*"
    r"(?:\((?P<flag>[A-Z*]+)\)\s*)?"
    r"(?P<unit>\S+)\s+\[(?P<low>-?\d+(?:\.\d+)?)-(?P<high>-?\d+(?:\.\d+)?)\]\s+"
    r"spec\s+(?P<spec>\S+)\s+status\s+(?P<status>\S+)\s*$"
)
_PROVIDER = re.compile(
    r"^(Attending|Ordering|Authorizing|Collecting RN|Pathologist):\s*(.+)$", re.I
)
_DIAG = re.compile(r"^Diagnosis:\s*([A-Z]\d{2}(?:\.\d+)?)\s+(.+)$")
_DISPOSITION = re.compile(r"^Disposition:\s*(.+)$", re.I)
_TRANSFER = re.compile(r"Transferred from\s+(\S+)", re.I)
_TRANSFUSION = re.compile(
    r"Order\s+(?P<oid>\S+)\s+Product:\s*(?P<product>\S+)\s+Units:\s*(?P<units>\d+)\s+"
    r"Indication:\s*(?P<ind>.+)$",
    re.I,
)

_ROLE_MAP = {
    "attending": "attending",
    "ordering": "ordering",
    "authorizing": "authorizing",
    "collecting rn": "collecting_rn",
    "pathologist": "pathologist",
}


def fingerprint(text: str) -> bool:
    """A confident Epic match: at least one `DD/MM/YYYY - <type> in <facility>` header."""
    return any(_HEADER.match(line.strip()) for line in text.splitlines())


# EMR export times are wall-clock with no zone; we normalize to UTC-aware so the
# arbiter's temporal comparisons (against a tz-aware capture anchor) are valid.
# Real per-facility timezone handling is a W3 refinement (§6.4 midnight/tz drift).
def _date(s: str) -> datetime:
    return datetime.strptime(s, "%m/%d/%Y").replace(tzinfo=UTC)


def _datetime(day: str, hm: str) -> datetime:
    return datetime.strptime(f"{day} {hm}", "%m/%d/%Y %H:%M").replace(tzinfo=UTC)


def _interpretation(flag: str | None) -> str | None:
    if not flag:
        return None
    if "*" in flag:
        return "critical"
    if "H" in flag.upper():
        return "high"
    if "L" in flag.upper():
        return "low"
    return "abnormal"


class _EncounterDraft:
    def __init__(
        self, key: str, type_phrase: str, facility: str, header_date: datetime, anchor: str
    ) -> None:
        self.key = key
        self.type_phrase = type_phrase
        self.facility = facility
        self.header_date = header_date
        self.anchor = anchor
        self.adm: datetime | None = None
        self.dc: datetime | None = None
        self.unit: str | None = None
        self.visit: datetime | None = None
        self.adm_dc_banners = 0
        self.visit_banners = 0
        self.disposition: str | None = None
        self.transferred_from: str | None = None
        self.providers: list[CandidateProvider] = []
        self.diagnoses: list[CandidateDiagnosis] = []
        self.transfusions: list[CandidateTransfusion] = []
        self.observations: list[CandidateObservation] = []


def parse_epic(text: str) -> ParseResult:
    """Parse a decrypted Epic report's extracted text into typed candidates."""
    result = ParseResult()
    drafts: list[_EncounterDraft] = []
    current: _EncounterDraft | None = None
    page = "page 1"
    pending_banners: list[tuple[str, re.Match[str]]] = []  # banners seen since the last header
    collected_at: datetime | None = None
    pathology_lines: list[str] = []
    in_pathology = False

    def attach_banner(draft: _EncounterDraft, kind: str, m: re.Match[str]) -> None:
        if kind == "admdc":
            draft.adm_dc_banners += 1
            draft.adm, draft.dc = _date(m.group(1)), _date(m.group(2))
            if m.group(3):
                draft.unit = m.group(3)
        else:
            draft.visit_banners += 1
            draft.visit = _date(m.group(1))

    for raw in text.splitlines():
        line = raw.rstrip()
        pm = _PAGE.match(line.strip())
        if pm:
            page = f"page {pm.group(1)}"
            continue

        if line.strip().upper().startswith("SURGICAL PATHOLOGY REPORT"):
            in_pathology = True
            if result.pathology_anchor is None:
                result.pathology_anchor = page  # cite the page the narrative opens on
        if in_pathology:
            pathology_lines.append(line)
            continue

        badmdc = _BANNER_ADMDC.search(line)
        bvisit = _BANNER_VISIT.search(line)
        header = _HEADER.match(line.strip())

        if header:
            current = _EncounterDraft(
                key=f"{page}:{header.group(1)}:{header.group(3)}",
                type_phrase=header.group(2),
                facility=header.group(3).strip(),
                header_date=_date(header.group(1)),
                anchor=page,
            )
            drafts.append(current)
            # Banner bleed: a banner printed ABOVE this header belongs to THIS
            # encounter (the header below it), not the previous one.
            for kind, m in pending_banners:
                attach_banner(current, kind, m)
            pending_banners = []
            collected_at = None
            continue

        # Banner bleed (§2.1): banners print ABOVE the header, so every banner is
        # deferred and attributed to the NEXT header below it — never the current
        # encounter. (Multi-page continuation banners are refined against real Epic
        # pages in W3 via the mode-of-banners rule; the synthetic corpus prints one
        # banner per encounter directly above its header.)
        if badmdc:
            pending_banners.append(("admdc", badmdc))
            continue
        if bvisit:
            pending_banners.append(("visit", bvisit))
            continue

        if current is None:
            continue

        cm = _COLLECTED.search(line)
        if cm:
            collected_at = _datetime(cm.group(1), cm.group(2))
            continue

        pv = _PROVIDER.match(line.strip())
        if pv:
            role = _ROLE_MAP[pv.group(1).lower()]
            current.providers.append(CandidateProvider(name=pv.group(2).strip(), role=role))
            continue

        dm = _DIAG.match(line.strip())
        if dm:
            current.diagnoses.append(
                CandidateDiagnosis(icd10=dm.group(1), label=dm.group(2).strip())
            )
            continue

        dispo = _DISPOSITION.match(line.strip())
        if dispo:
            current.disposition = dispo.group(1).strip()
            continue

        tf = _TRANSFER.search(line)
        if tf:
            current.transferred_from = tf.group(1)
            continue

        tx = _TRANSFUSION.search(line)
        if tx:
            current.transfusions.append(
                CandidateTransfusion(
                    order_id=tx.group("oid"),
                    product=tx.group("product"),
                    units=int(tx.group("units")),
                    indication=tx.group("ind").strip(),
                )
            )
            continue

        om = _OBS.match(line)
        if om and collected_at is not None:
            current.observations.append(_observation(om, collected_at, current.facility, page))
            continue

    result.encounters = [_finalize(d) for d in drafts]
    _link_transfers(result.encounters, drafts)
    if pathology_lines:
        result.pathology_narrative = "\n".join(pathology_lines).strip()
    return result


def _observation(
    m: re.Match[str], collected_at: datetime, facility: str, anchor: str
) -> CandidateObservation:
    return CandidateObservation(
        analyte=canonicalize_analyte(m.group("analyte").strip()),
        collected_at=collected_at,
        value_num=float(m.group("value")),
        value_text=None,
        unit=m.group("unit"),
        ref_low=float(m.group("low")),
        ref_high=float(m.group("high")),
        ref_text=None,
        interpretation=_interpretation(m.group("flag")),
        specimen_id=m.group("spec"),
        performing_lab=None,  # Epic labs are in-house; no per-draw performer printed
        fhir_status=m.group("status").lower(),
        source_system=SOURCE_SYSTEM,
        fidelity=FIDELITY_TEXT_LAYER,
        source_anchor=anchor,
    )


def _finalize(d: _EncounterDraft) -> CandidateEncounter:
    # inpatient iff Adm/DC banners outnumber Visit-date banners (§6.3).
    inpatient = d.adm_dc_banners > d.visit_banners
    enc_class = "inpatient" if inpatient else "ambulatory"
    admitted = d.adm if inpatient else d.visit
    discharged = d.dc if inpatient else d.visit
    return CandidateEncounter(
        key=d.key,
        encounter_class=enc_class,
        facility=d.facility,
        care_unit=d.unit if inpatient else None,
        admitted_at=admitted,
        discharged_at=discharged,
        disposition=d.disposition,
        source_system=SOURCE_SYSTEM,
        source_anchor=d.anchor,
        providers=d.providers,
        diagnoses=d.diagnoses,
        transfusions=d.transfusions,
        observations=d.observations,
    )


def _link_transfers(encounters: list[CandidateEncounter], drafts: list[_EncounterDraft]) -> None:
    """A facility transfer is two inpatient encounters whose admit == the prior's
    discharge; the later segment's `part_of_key` points at the episode's first."""
    by_key = {e.key: e for e in encounters}
    for enc, d in zip(encounters, drafts, strict=True):
        if enc.encounter_class != "inpatient" or enc.admitted_at is None or not d.transferred_from:
            continue
        for other in encounters:
            if (
                other.key != enc.key
                and other.encounter_class == "inpatient"
                and other.discharged_at == enc.admitted_at
            ):
                # Walk to the episode's FIRST segment (earliest admit in the chain).
                first = other
                seen = {enc.key}
                while first.part_of_key and first.part_of_key in by_key and first.key not in seen:
                    seen.add(first.key)
                    first = by_key[first.part_of_key]
                enc.part_of_key = first.key
                break
