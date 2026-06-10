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


@dataclass(frozen=True)
class FactView:
    """The slice of an existing fact row that supersession decisions read."""

    id: str
    kind: str
    statement: str
    value_json: dict[str, Any] | None
    object_entity_id: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    reported_at: datetime
    status: str
    pinned: bool


@dataclass(frozen=True)
class Candidate:
    """A newly extracted fact, before any row exists for it."""

    kind: str
    statement: str
    value_json: dict[str, Any] | None
    object_entity_id: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    reported_at: datetime


@dataclass
class Decision:
    """What the pipeline must do with the candidate.

    Exactly one of refresh_id / insert is set. supersede_ids close and chain
    old facts onto the new row; hold_ids move old facts to pending_review
    (attribute collisions hold BOTH sides).
    """

    refresh_id: str | None = None
    insert: bool = False
    insert_status: str = "active"
    insert_superseded_by: str | None = None
    insert_valid_to: datetime | None = None
    supersede_ids: list[str] = field(default_factory=list)
    hold_ids: list[str] = field(default_factory=list)
    review_kind: str | None = None
    review_extra: dict[str, Any] = field(default_factory=dict)
    conflicting_id: str | None = None


def values_equal(candidate: Candidate, existing: FactView) -> bool:
    """Same value = structured payload if either side has one, else the
    object entity for pure edges, else the rendered statement."""
    if candidate.object_entity_id != existing.object_entity_id:
        return False
    if candidate.value_json is not None or existing.value_json is not None:
        return candidate.value_json == existing.value_json
    if candidate.object_entity_id is not None:
        return True
    return candidate.statement.strip() == existing.statement.strip()


def _validity(valid_from: datetime | None, reported_at: datetime) -> tuple[datetime, datetime]:
    # "Newest" = latest validity, tie-broken by reported_at among facts about
    # the same validity period; facts without validity fall back to capture.
    return (valid_from or reported_at, reported_at)


def decide(candidate: Candidate, existing: list[FactView], *, predicate: str = "") -> Decision:
    """Resolve one candidate against the identity key's existing facts."""
    live = [e for e in existing if e.status != "retracted"]

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
        if candidate.kind == "preference":
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
