"""Per-kind fact supersession decisions (docs/ANALYSIS.md "Fact kinds").

Pure logic over fact views: the pipeline loads the identity key's existing
facts, asks `decide`, and applies the returned actions in its transaction.
Two invariants hold for every kind:

- supersession compares VALIDITY time (valid_from, tie-broken by reported_at),
  never capture time — a retrospective note about 2019 lands as history, not
  as the new current value;
- a pinned fact is a human override: it is never auto-superseded or held,
  only re-flagged via a review item.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Functional relationship predicates: at most one current value, so a new
# binding supersedes (state semantics). Everything else accumulates.
# Spec allowlist is employer/spouse/residence; the schema.org spellings the
# prompt steers toward are included so identity keys still match.
FUNCTIONAL_PREDICATES = frozenset(
    {"employer", "worksfor", "works_for", "spouse", "residence", "homelocation", "home_location"}
)


def is_functional(predicate: str) -> bool:
    return predicate.lower() in FUNCTIONAL_PREDICATES


# Schedule-binding predicates: an appointment's time is a binding whose value
# IS a validity instant, so ordering by validity would make a reschedule to an
# EARLIER time lose to the time it replaces. The newest INSTRUCTION wins
# regardless of direction (docs/ANALYSIS.md "Temporal tokens and appointment
# identity"), so these order by reported_at. The set carries the schema.org
# spelling the prompt steers toward plus its snake_case twin, like
# FUNCTIONAL_PREDICATES above.
SCHEDULE_PREDICATES = frozenset({"scheduledtime", "scheduled_time"})


def is_schedule_binding(predicate: str) -> bool:
    return predicate.lower() in SCHEDULE_PREDICATES


# Below this, a candidate never auto-supersedes a MORE confident active fact;
# it parks in pending_review behind a low_confidence card instead.
# docs/ANALYSIS.md "Guards" demands this for low-confidence numeric health
# facts (OCR-derived especially); we apply it domain-agnostically because
# decide() is deliberately pure (it never sees the fact's domain) and the
# asymmetry is safe — over-guarding a general fact costs one review card,
# under-guarding a health fact is a silent overwrite by garbage.
LOW_CONFIDENCE = 0.5


@dataclass(frozen=True)
class FactView:
    """The slice of an existing fact row that supersession decisions read."""

    id: str
    kind: str
    statement: str
    value_json: dict[str, Any] | None
    object_entity_id: str | None
    assertion: str
    valid_from: datetime | None
    valid_to: datetime | None
    reported_at: datetime
    status: str
    pinned: bool
    # Default 1.0: rows predating the confidence column (NULL) are treated as
    # confident, so a low-confidence candidate still cannot displace them.
    confidence: float = 1.0


@dataclass(frozen=True)
class Candidate:
    """A newly extracted fact, before any row exists for it."""

    kind: str
    statement: str
    value_json: dict[str, Any] | None
    object_entity_id: str | None
    assertion: str
    valid_from: datetime | None
    valid_to: datetime | None
    reported_at: datetime
    confidence: float = 1.0


@dataclass
class Decision:
    """What the pipeline must do with the candidate.

    Exactly one of refresh_id / close_id / insert is set. close_id is an
    in-place interval close: the candidate merely supplies the END of the
    existing open interval (same value/object, same valid_from), so the
    pipeline UPDATEs that row instead of chaining a duplicate. supersede_ids
    close and chain old facts onto the new row; hold_ids move old facts to
    pending_review (attribute collisions hold BOTH sides).
    """

    refresh_id: str | None = None
    close_id: str | None = None
    close_valid_to: datetime | None = None
    insert: bool = False
    insert_status: str = "active"
    insert_superseded_by: str | None = None
    insert_valid_to: datetime | None = None
    supersede_ids: list[str] = field(default_factory=list)
    hold_ids: list[str] = field(default_factory=list)
    review_kind: str | None = None
    review_extra: dict[str, Any] = field(default_factory=dict)
    conflicting_id: str | None = None


# Unit normalization exists ONLY so values_equal can recognize the same
# measurement re-expressed in another unit (180 lb restated as 81.6 kg at the
# same instant is a refresh, not a fact_conflict). Values are STORED verbatim.
# The table is deliberately tiny — the units personal notes actually flip
# between, with exact conversion factors — because every entry widens what the
# pipeline silently treats as "the same value"; anything unlisted keeps the
# conservative conflict path and a human decides.
_KG_PER_LB = 0.45359237
_CM_PER_IN = 2.54
_UNIT_TO_BASE: dict[str, tuple[str, Callable[[float], float]]] = {
    "kg": ("mass", lambda v: v),
    "lb": ("mass", lambda v: v * _KG_PER_LB),
    "lbs": ("mass", lambda v: v * _KG_PER_LB),
    "cm": ("length", lambda v: v),
    "in": ("length", lambda v: v * _CM_PER_IN),
    "ft": ("length", lambda v: v * 12 * _CM_PER_IN),
    "°c": ("temperature", lambda v: v),
    "c": ("temperature", lambda v: v),
    "°f": ("temperature", lambda v: (v - 32) * 5 / 9),
    "f": ("temperature", lambda v: (v - 32) * 5 / 9),
}

# A re-expressed value is typically rounded to ~3 significant figures
# (180 lb -> "81.6 kg", true value 81.65), so equality allows that much float
# slack; abs_tol covers exact-zero readings (0 °C vs 32 °F).
_QUANTITY_REL_TOL = 1e-3


def _quantity(value_json: dict[str, Any] | None) -> tuple[str, float, dict[str, Any]] | None:
    """(dimension, base-unit value, remaining keys) for a {value, unit} shaped
    payload in a convertible unit; None means 'not comparable this way'."""
    if not isinstance(value_json, dict):
        return None
    value, unit = value_json.get("value"), value_json.get("unit")
    if isinstance(value, bool) or not isinstance(value, int | float) or not isinstance(unit, str):
        return None
    base = _UNIT_TO_BASE.get(unit.strip().lower())
    if base is None:
        return None
    dimension, to_base = base
    rest = {k: v for k, v in value_json.items() if k not in ("value", "unit")}
    return dimension, to_base(float(value)), rest


def _same_quantity(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    qa, qb = _quantity(a), _quantity(b)
    if qa is None or qb is None or qa[0] != qb[0]:
        return False
    # Any keys beyond value/unit (site, device, ...) must still match exactly:
    # a qualifierless payload and a qualified one are not the same reading.
    if qa[2] != qb[2]:
        return False
    return math.isclose(qa[1], qb[1], rel_tol=_QUANTITY_REL_TOL, abs_tol=1e-9)


def values_equal(candidate: Candidate, existing: FactView) -> bool:
    """Same value = structured payload if either side has one, else the
    object entity for pure edges, else the rendered statement.

    A flipped assertion (asserted<->negated, etc.) is never an idempotent
    refresh: "I no longer own X" carries the same object as "I own X" but
    asserts the inverse, so it must fall through to the per-kind supersession
    logic — the refresh path only writes rendering/provenance, never assertion,
    and would otherwise leave a head asserting the opposite of the truth
    (docs/ANALYSIS.md "Assertion status")."""
    if candidate.object_entity_id != existing.object_entity_id:
        return False
    if candidate.assertion != existing.assertion:
        return False
    if candidate.value_json is not None or existing.value_json is not None:
        if candidate.value_json == existing.value_json:
            return True
        return _same_quantity(candidate.value_json, existing.value_json)
    if candidate.object_entity_id is not None:
        return True
    return candidate.statement.strip() == existing.statement.strip()


def _validity(valid_from: datetime | None, reported_at: datetime) -> tuple[datetime, datetime]:
    # "Newest" = latest validity, tie-broken by reported_at among facts about
    # the same validity period; facts without validity fall back to capture.
    return (valid_from or reported_at, reported_at)


def _interval_close(candidate: Candidate, live: list[FactView], predicate: str) -> Decision | None:
    """A retrospective end-date backfill ("I actually left Acme back in March")
    is not a value change: the candidate restates the SAME open state — same
    object/value, same valid_from — and merely supplies the valid_to the open
    row lacks. Closing that interval in place keeps one row whose old fact
    "stays true about its interval" (docs/ANALYSIS.md state row / SCD-2);
    chaining a duplicate or filing a conflict would dispute a fact nobody
    disputes. Checked BEFORE the refresh path, which writes only rendering and
    provenance and would otherwise swallow the new end date."""
    if candidate.valid_to is None:
        return None
    if candidate.kind != "state" and not (
        candidate.kind == "relationship" and is_functional(predicate)
    ):
        return None
    for e in live:
        # Pinned rows are human overrides: fall through to the per-kind logic,
        # which re-flags instead of editing them.
        if e.status != "active" or e.valid_to is not None or e.pinned:
            continue
        if e.assertion != candidate.assertion or e.valid_from != candidate.valid_from:
            continue
        # For a pure edge the object IS the value (value_json only carries
        # start/end markers, which legitimately change on a close).
        same_edge = (
            candidate.object_entity_id is not None
            and candidate.object_entity_id == e.object_entity_id
        )
        if same_edge or values_equal(candidate, e):
            return Decision(close_id=e.id, close_valid_to=candidate.valid_to)
    return None


def decide(candidate: Candidate, existing: list[FactView], *, predicate: str = "") -> Decision:
    """Resolve one candidate against the identity key's existing facts."""
    live = [e for e in existing if e.status != "retracted"]

    closed = _interval_close(candidate, live, predicate)
    if closed is not None:
        return closed

    # Re-extraction idempotency: an identical value refreshes provenance in
    # place — citations survive, no chain link, no review noise. Accumulating
    # kinds are time-series, so "identical" includes the instant; a superseded
    # row only counts as identical at the same validity (re-asserting an OLD
    # value with NEW validity is a genuine transition, e.g. moving back to a
    # previous address, and must fall through to the per-kind logic).
    accumulating = candidate.kind in ("event", "measurement")
    for e in live:
        if not values_equal(candidate, e):
            continue
        same_validity = e.valid_from == candidate.valid_from
        if accumulating and same_validity:
            return Decision(refresh_id=e.id)
        if not accumulating and (e.status in ("active", "pending_review") or same_validity):
            return Decision(refresh_id=e.id)

    if candidate.kind in ("event", "measurement"):
        clash = next(
            (
                e
                for e in live
                if e.status in ("active", "pending_review")
                and e.valid_from is not None
                and e.valid_from == candidate.valid_from
            ),
            None,
        )
        if clash is not None:
            # Same metric/event, same instant, different value: an extraction
            # error somewhere — never auto-supersede, a human picks.
            return Decision(
                insert=True,
                insert_status="pending_review",
                review_kind="fact_conflict",
                conflicting_id=clash.id,
            )
        return Decision(insert=True)  # time-series accumulate

    if candidate.kind == "attribute":
        heads = [e for e in live if e.status in ("active", "pending_review")]
        if not heads:
            return Decision(insert=True)
        current = max(heads, key=lambda e: _validity(e.valid_from, e.reported_at))
        if current.pinned:
            return Decision(
                insert=True,
                insert_status="pending_review",
                review_kind="attribute_collision",
                conflicting_id=current.id,
            )
        # Two birthdays is a bug, not news: BOTH sides go to pending_review.
        return Decision(
            insert=True,
            insert_status="pending_review",
            hold_ids=[e.id for e in heads if e.status == "active"],
            review_kind="attribute_collision",
            conflicting_id=current.id,
        )

    if candidate.kind == "relationship" and not is_functional(predicate):
        return Decision(insert=True)  # non-functional edges accumulate

    # state / preference / functional relationship: single current value.
    actives = [e for e in live if e.status == "active"]
    if not actives:
        return Decision(insert=True)

    def key(valid_from: datetime | None, reported_at: datetime) -> tuple[datetime, datetime]:
        # Preferences are valid from when reported — newest report wins.
        # Schedule bindings also order by reported_at: the latest reschedule
        # instruction wins even when it moves the time earlier.
        if candidate.kind == "preference" or is_schedule_binding(predicate):
            return (reported_at, reported_at)
        return _validity(valid_from, reported_at)

    current = max(actives, key=lambda e: key(e.valid_from, e.reported_at))
    if key(candidate.valid_from, candidate.reported_at) >= key(
        current.valid_from, current.reported_at
    ):
        if current.pinned:
            # Re-flag, never flip: the human decision survives reprocessing.
            return Decision(
                insert=True,
                insert_status="pending_review",
                review_kind="fact_conflict",
                conflicting_id=current.id,
            )
        if candidate.confidence < LOW_CONFIDENCE and candidate.confidence < current.confidence:
            # A blurry OCR read must not silently overwrite confident
            # knowledge: park the candidate, keep the current fact active,
            # and let a human adjudicate via the low_confidence card.
            return Decision(
                insert=True,
                insert_status="pending_review",
                review_kind="low_confidence",
                conflicting_id=current.id,
            )
        return Decision(
            insert=True,
            supersede_ids=[current.id],
            review_kind="fact_conflict",
            review_extra={"urgency": "low"} if candidate.kind == "preference" else {},
            conflicting_id=current.id,
        )
    # Retrospective: the candidate is about an older validity period — it
    # lands as already-closed history and must not displace the current value.
    return Decision(
        insert=True,
        insert_status="superseded",
        insert_superseded_by=current.id,
        insert_valid_to=candidate.valid_to or current.valid_from,
    )
