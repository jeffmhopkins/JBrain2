"""The ARIA portal-reprint parser (docs/plans/EMR_IMPORT_PLAN.md §6.2/§6.3).

ARIA pages are scanned images with NO text layer: the vision-LLM OCR job
(`OCR_CONFIDENCE = 0.7`, §6.2) yields linear text, so this parser is
line-oriented — no word geometry. It is a reprint of the same 2021 labs
OneContent already carries, so most reads reconcile INTO the precise OneContent
draw (§6.4, the next stage); a read that matches nothing parks in review there.
This parser's only job is faithful, non-guessing extraction of the OCR text.

Two OCR realities are respected, not papered over: the `Collected` line prints a
**date only** (so each draw's precision is `day`, never a fabricated time), and
OCR noise (dot leaders between label and value; an `O`-for-`0` in a reference
range) is tolerated where it is harmless and left VERBATIM where "correcting" it
would be a guess — a range that fails to parse keeps its raw text, it is never
invented. Reads are **orphan** (encounter-less, §3.4 note): ARIA prints no
ordering provider, so `encounter_id` stays NULL rather than inventing a visit.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from jbrain.ingest.emr.candidates import (
    CandidateObservation,
    ParseResult,
    canonicalize_analyte,
)

SOURCE_SYSTEM = "aria"
FIDELITY_OCR = 1  # lower than a text layer — the §6.4 tie-break loser
OCR_CONFIDENCE = 0.7  # the vision-OCR extract's confidence (§6.2); applied at lowering
FHIR_STATUS = "final"

_PAGE = re.compile(r"^---\s*page\s+(\d+)\s*---\s*$", re.I)
_COLLECTED = re.compile(r"^Collected\s+(\d{2}/\d{2}/\d{4})\s*$", re.I)
_NUMERIC = re.compile(r"^-?\d+(?:\.\d+)?$")
_FLAG = re.compile(r"^[HL*]+$")
_REF = re.compile(r"^\((?P<body>.*)\)$")
_REF_RANGE = re.compile(r"^(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)$")


def fingerprint(text: str) -> bool:
    """A confident ARIA match: the portal banner (post-OCR text)."""
    return bool(re.search(r"\bARIA\b", text))


def _date(day: str) -> datetime:
    # Date-only OCR read: midnight UTC, precision `day` (set on the candidate).
    return datetime.strptime(day, "%m/%d/%Y").replace(tzinfo=UTC)


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


def _row(line: str, collected: datetime, anchor: str) -> CandidateObservation | None:
    """Parse one OCR result line: `<analyte> <dot-leaders> <value> [flag] <unit>
    (<ref>)`. Dot-leader tokens are dropped; a value must be numeric or the line is
    not a result row (a header/banner is skipped, never coerced)."""
    tokens = [t for t in line.split() if t != "."]
    value_idx = next((i for i, t in enumerate(tokens) if _NUMERIC.match(t)), None)
    if value_idx is None or value_idx == 0:
        return None
    analyte = " ".join(tokens[:value_idx]).strip()
    rest = tokens[value_idx + 1 :]
    if not rest:
        return None
    value_num = float(tokens[value_idx])

    flag = None
    if _FLAG.match(rest[0]):
        flag, rest = rest[0], rest[1:]
    if not rest:
        return None
    unit = rest[0]

    ref_low = ref_high = None
    ref_text = None
    ref_m = _REF.match(rest[-1]) if len(rest) > 1 else None
    if ref_m:
        body = ref_m.group("body").strip()
        rng = _REF_RANGE.match(body)
        if rng:
            ref_low, ref_high = float(rng.group(1)), float(rng.group(2))
        else:
            ref_text = body  # OCR-garbled range (e.g. "4.O-11.0") kept verbatim, never guessed
    return CandidateObservation(
        analyte=canonicalize_analyte(analyte),
        collected_at=collected,
        value_num=value_num,
        value_text=None,
        unit=unit,
        ref_low=ref_low,
        ref_high=ref_high,
        ref_text=ref_text,
        interpretation=_interpretation(flag),
        specimen_id="",  # OCR reprint prints no specimen id
        performing_lab=None,
        fhir_status=FHIR_STATUS,
        source_system=SOURCE_SYSTEM,
        fidelity=FIDELITY_OCR,
        source_anchor=anchor,
        precision="day",  # date-only OCR timestamp
    )


def parse_aria(text: str) -> ParseResult:
    """Parse OCR'd ARIA text into orphan (encounter-less) observations, one per
    readable result line, dated by the most recent `Collected` line."""
    result = ParseResult()
    page = "page 1"
    collected: datetime | None = None
    for raw in text.splitlines():
        line = raw.strip()
        pm = _PAGE.match(line)
        if pm:
            page = f"page {pm.group(1)}"
            continue
        cm = _COLLECTED.match(line)
        if cm:
            collected = _date(cm.group(1))
            continue
        if collected is None or not line or line.startswith("["):
            continue  # a banner, note, or a row before its Collected date
        obs = _row(line, collected, page)
        if obs is not None:
            result.orphan_observations.append(obs)
    return result
