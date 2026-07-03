"""The OneContent cumulative-lab parser (docs/plans/EMR_IMPORT_PLAN.md §6.2/§6.3).

OneContent prints 82 pages of **account-keyed** fixed-width cumulative tables — the
hardest parse in the corpus. `get_text("text")` reflows inter-column whitespace, so
character-offset column slicing misaligns (the §6.2 go/no-go gate resolved to
*geometry*). This parser is therefore a pure function of **word boxes**
(`get_text("words")` x-geometry — still PyMuPDF, no new dependency): it groups
words into lines by y, derives each table's column bands from its header row's word
x-positions, and assigns every word to a band by x0. Right-aligned values and
multi-word analyte names ("White Blood Cell Count") that a char ruler would split
land in the correct column because a band is an x-RANGE, not a point.

The spine is the `Account:` number (§6.3), not admit/discharge — each account is
one **ambulatory lab visit** (§3.4 note): a `CandidateEncounter(class="ambulatory")`
enclosing its draws, so the account's orderer (when printed) has an Encounter to
hang on. Each row's `Collected` timestamp sets that draw's `valid_from` (§3.3); the
abnormal-flag legend ("H=High L=Low *=Critical") drives `interpretation`. Postal
addresses/geo are never emitted (§3.6 Layer 1) — only the facility NAME rides a
candidate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from jbrain.ingest.emr.candidates import (
    CandidateEncounter,
    CandidateObservation,
    ParseResult,
    canonicalize_analyte,
)

SOURCE_SYSTEM = "onecontent"
FIDELITY_TEXT_LAYER = 2
FHIR_STATUS = "final"  # cumulative reports print settled (final) results

_ACCOUNT = re.compile(r"Account:\s*(C\d+)", re.I)
_LEGEND = re.compile(r"KEY FOR ABNORMAL COLUMN", re.I)
_LEGEND_PAIR = re.compile(r"(\S+)\s*=\s*(\w+)")
_REF_RANGE = re.compile(r"^(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)$")
_COLLECTED = re.compile(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})")
_Y_TOL = 3.0  # words within this many points of y0 are one visual line

# Header labels → the canonical column name, in print order. "Range" is folded into
# the `ref` column (the header prints "Ref Range" as two words, one column).
_HEADER_LABELS = {
    "analyte": "analyte",
    "result": "result",
    "ab": "flag",
    "units": "units",
    "ref": "ref",
    "collected": "collected",
}
_DEFAULT_LEGEND = {"h": "high", "l": "low", "*": "critical"}


@dataclass(frozen=True)
class WordBox:
    """One `get_text("words")` token: its bounding box and text. `block`/`line`/
    `word_no` from PyMuPDF are not needed — geometry (x0, y0) carries the layout."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str


def fingerprint(text: str) -> bool:
    """A confident OneContent match: the abnormal-column legend or an `Account:` key."""
    return bool(_LEGEND.search(text) or _ACCOUNT.search(text))


def _date(day: str, hm: str) -> datetime:
    # Wall-clock export time normalized to UTC-aware (mirrors epic._datetime) so the
    # arbiter's comparisons against a tz-aware anchor are valid.
    return datetime.strptime(f"{day} {hm}", "%m/%d/%Y %H:%M").replace(tzinfo=UTC)


def _group_lines(words: list[WordBox]) -> list[list[WordBox]]:
    """Group a page's words into visual lines by y0 (rows print at a shared y), each
    line left-to-right by x0. Robust to minor y jitter within a printed row."""
    lines: list[list[WordBox]] = []
    for w in sorted(words, key=lambda b: (b.y0, b.x0)):
        if lines and abs(w.y0 - lines[-1][0].y0) <= _Y_TOL:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda b: b.x0)
    return lines


def _line_text(line: list[WordBox]) -> str:
    return " ".join(w.text for w in line)


def _column_bands(header: list[WordBox]) -> list[tuple[str, float]] | None:
    """Derive (column_name, left_x) bands from a header line's word x-positions. A
    band runs from its left_x up to the next band's left_x; the last band is
    open-ended. None if the line is not a recognizable table header."""
    bands: list[tuple[str, float]] = []
    for w in header:
        name = _HEADER_LABELS.get(w.text.strip().lower())
        if name is not None and name not in {n for n, _ in bands}:
            bands.append((name, w.x0))
    names = {n for n, _ in bands}
    if not {"analyte", "result", "collected"} <= names:
        return None  # not the data header (needs at least analyte/result/collected)
    return sorted(bands, key=lambda b: b[1])


def _slice_row(line: list[WordBox], bands: list[tuple[str, float]]) -> dict[str, str]:
    """Assign each word to the band whose x-range contains its x0, joining words that
    share a band (multi-word analyte names, `date time` collected). This is the
    geometry recovery a char-offset ruler cannot do on reflowed text (§6.2)."""
    edges = [x for _, x in bands]
    out: dict[str, list[str]] = {name: [] for name, _ in bands}
    for w in line:
        idx = 0
        for i, left in enumerate(edges):
            if w.x0 + 0.5 >= left:  # tolerate sub-point rounding at a band's left edge
                idx = i
        out[bands[idx][0]].append(w.text)
    return {name: " ".join(parts).strip() for name, parts in out.items()}


def _parse_legend(text: str) -> dict[str, str]:
    """Parse the report's abnormal-flag legend into a flag-char → interpretation map,
    lower-cased. Falls back to the standard H/L/* map when the line is absent."""
    m = _LEGEND.search(text)
    if not m:
        return dict(_DEFAULT_LEGEND)
    tail = text[m.end() : text.find("\n", m.end()) if "\n" in text[m.end() :] else len(text)]
    legend = {sym.lower(): word.lower() for sym, word in _LEGEND_PAIR.findall(tail)}
    return legend or dict(_DEFAULT_LEGEND)


def _interpretation(flag: str, legend: dict[str, str]) -> str | None:
    """Map an abnormal flag ("L", "L*", "H") to an interpretation via the legend.
    A critical star wins over a high/low qualifier (the most severe reading holds)."""
    flag = flag.strip()
    if not flag:
        return None
    if "*" in flag and "*" in legend:
        return legend["*"]
    for ch in flag.lower():
        if ch in legend:
            return legend[ch]
    return "abnormal"


def _observation(
    row: dict[str, str], collected_at: datetime, interpretation: str | None, anchor: str
) -> CandidateObservation | None:
    """Build one draw from a sliced row. None when the result cell is not numeric —
    a header echo or a note line is skipped, never coerced into a spurious value."""
    analyte = row.get("analyte", "").strip()
    result = row.get("result", "").strip()
    if not analyte or not result:
        return None
    try:
        value_num = float(result)
    except ValueError:
        return None
    ref_low = ref_high = None
    rm = _REF_RANGE.match(row.get("ref", "").strip())
    if rm:
        ref_low, ref_high = float(rm.group(1)), float(rm.group(2))
    return CandidateObservation(
        analyte=canonicalize_analyte(analyte),
        collected_at=collected_at,
        value_num=value_num,
        value_text=None,
        unit=row.get("units", "").strip() or None,
        ref_low=ref_low,
        ref_high=ref_high,
        ref_text=None,
        interpretation=interpretation,
        specimen_id="",  # OneContent prints no per-draw specimen id
        performing_lab=None,
        fhir_status=FHIR_STATUS,
        source_system=SOURCE_SYSTEM,
        fidelity=FIDELITY_TEXT_LAYER,
        source_anchor=anchor,
        precision="instant",
    )


def parse_onecontent(pages: list[list[WordBox]], *, legend_text: str = "") -> ParseResult:
    """Parse OneContent word-box pages into account-keyed ambulatory encounters.

    `legend_text` is the decrypted report text used only to read the abnormal-flag
    legend (which prints once, in the title region a table-only word view may omit);
    column recovery itself is pure geometry.
    """
    legend = _parse_legend(legend_text)
    result = ParseResult()
    by_account: dict[str, CandidateEncounter] = {}
    order: list[str] = []
    account: str | None = None
    bands: list[tuple[str, float]] | None = None

    for page_idx, words in enumerate(pages):
        anchor = f"page {page_idx + 1}"
        for line in _group_lines(words):
            text = _line_text(line)
            am = _ACCOUNT.search(text)
            if am:
                acct: str = am.group(1)
                account = acct
                bands = None  # each account reprints its own header
                if acct not in by_account:
                    by_account[acct] = CandidateEncounter(
                        key=acct,
                        encounter_class="ambulatory",
                        facility=None,
                        care_unit=None,
                        admitted_at=None,
                        discharged_at=None,
                        disposition=None,
                        source_system=SOURCE_SYSTEM,
                        source_anchor=anchor,
                    )
                    order.append(acct)
                continue
            header = _column_bands(line)
            if header is not None:
                bands = header
                continue
            if bands is None or account is None:
                continue  # a title/legend line, or a row before its header
            row = _slice_row(line, bands)
            cm = _COLLECTED.search(row.get("collected", ""))
            if not cm:
                continue
            collected_at = _date(cm.group(1), cm.group(2))
            interp = _interpretation(row.get("flag", ""), legend)
            obs = _observation(row, collected_at, interp, anchor)
            if obs is None:
                continue
            by_account[account].observations.append(obs)

    # An account's ambulatory visit spans the day(s) its draws were collected.
    for account in order:
        enc = by_account[account]
        draws = [o.collected_at for o in enc.observations]
        if draws:
            enc.admitted_at = min(draws)
            enc.discharged_at = max(draws)
    result.encounters = [by_account[a] for a in order]
    return result
