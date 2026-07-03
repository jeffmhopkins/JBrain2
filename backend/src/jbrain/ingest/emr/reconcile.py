"""Cross-source candidate reconciliation — the dedup ENFORCER
(docs/plans/EMR_IMPORT_PLAN.md §6.4).

The 2021 labs recur across OneContent (precise: datetime + accession) and ARIA
(a date-only, specimen-less OCR reprint). The §3.3 qualifier is NOT the matcher —
the same physical draw renders as *different* raw qualifiers across sources. They
converge only because this **pre-graph, pure-Python** step forces a match first.

An OCR read reconciles to a precise draw only when ALL hold: (a) the **same
canonical analyte code** (§6.3, not the raw label); (b) the OCR date falls within
a **±1-day window** of the precise draw (absorbs midnight/timezone drift on a
portal reprint); and (c) the **values agree** within a per-analyte tolerance
(exact for integer counts, a small relative epsilon for float chemistries). On a
match the OCR read **adopts the precise draw's timestamp + specimen** (so its
minted qualifier equals the precise draw's — idempotent, dual-cited) and the
precise, higher-`fidelity` draw is authoritative. A read that matches **nothing**
— including a readable-but-WRONG timestamp that lands on a day with no compatible
draw — **parks in `pending_review`** behind a `low_confidence` card rather than
minting a spurious time-series point or guessing. This is the conscious
isolation-over-deduplication stance: an unmatched reprint is never independently
trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from jbrain.ingest.emr.candidates import CandidateObservation

REVIEW_KIND = "low_confidence"
PARK_SUBKIND = "ocr_unreconciled"
WINDOW_DAYS = 1  # ±1 calendar day around the precise draw
REL_EPSILON = 0.005  # 0.5% relative tolerance for float chemistries


@dataclass(frozen=True)
class Corroboration:
    """A precise draw and the OCR read that corroborates it, the OCR read rewritten
    to ADOPT the precise timestamp + specimen — so `ocr.qualifier == precise.qualifier`
    and lowering upserts one dual-cited fact (§6.4), never a duplicate draw."""

    precise: CandidateObservation
    ocr: CandidateObservation


@dataclass(frozen=True)
class ParkedRead:
    """An OCR read that matched no precise draw — held for review, never a fact."""

    observation: CandidateObservation
    reason: str
    subkind: str = PARK_SUBKIND


@dataclass(frozen=True)
class Reconciliation:
    """`to_lower` is the authoritative draw set (precise sources); `corroborated`
    records the dual-citation provenance; `parked` are the OCR reads to card."""

    to_lower: list[CandidateObservation]
    corroborated: list[Corroboration]
    parked: list[ParkedRead]


def _values_agree(a: float | None, b: float | None) -> bool:
    """Exact for integer counts (a misread digit must not silently merge); a small
    relative epsilon for float chemistries. A missing value never agrees."""
    if a is None or b is None:
        return False
    if a == int(a) and b == int(b):
        return a == b
    return abs(a - b) <= max(abs(a), abs(b)) * REL_EPSILON


def _within_window(ocr: CandidateObservation, precise: CandidateObservation) -> int | None:
    """The absolute day gap if within ±WINDOW_DAYS, else None."""
    gap = abs((ocr.collected_at.date() - precise.collected_at.date()).days)
    return gap if gap <= WINDOW_DAYS else None


def _best_match(
    ocr: CandidateObservation, precise: list[CandidateObservation]
) -> CandidateObservation | None:
    """The precise draw an OCR read reconciles to: same canonical code, in the
    ±1-day window, value in tolerance — the nearest day then the closest value wins
    (a same-day exact corroboration always beats a next-day one)."""
    best: tuple[int, float, CandidateObservation] | None = None
    for p in precise:
        if p.analyte.code != ocr.analyte.code:
            continue
        gap = _within_window(ocr, p)
        if gap is None or not _values_agree(ocr.value_num, p.value_num):
            continue
        vdiff = abs((ocr.value_num or 0.0) - (p.value_num or 0.0))
        key = (gap, vdiff, p)
        if best is None or key[:2] < best[:2]:
            best = key
    return best[2] if best is not None else None


def reconcile(
    precise: list[CandidateObservation], ocr: list[CandidateObservation]
) -> Reconciliation:
    """Reconcile OCR reads against precise draws. Precise draws are always
    authoritative and pass through unchanged; each OCR read either corroborates a
    precise draw (adopting its timestamp + specimen) or parks in review."""
    corroborated: list[Corroboration] = []
    parked: list[ParkedRead] = []
    for read in ocr:
        match = _best_match(read, precise)
        if match is None:
            parked.append(
                ParkedRead(
                    observation=read,
                    reason=(
                        f"no precise {read.analyte.name} draw within "
                        f"±{WINDOW_DAYS}d of {read.collected_at.date().isoformat()} "
                        "with an in-tolerance value"
                    ),
                )
            )
            continue
        # Adopt the precise draw's address so the reconciled qualifier is identical
        # (idempotent upsert, dual-cited); the OCR read never becomes a rival draw.
        adopted = replace(
            read,
            collected_at=match.collected_at,
            specimen_id=match.specimen_id,
            precision=match.precision,
        )
        corroborated.append(Corroboration(precise=match, ocr=adopted))
    return Reconciliation(to_lower=list(precise), corroborated=corroborated, parked=parked)
