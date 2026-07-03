"""Per-kind fact supersession decisions (docs/reference/ANALYSIS.md "Fact kinds").

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

# Residual raw-spelling allowlist for functional concepts the registry models as
# reified role-edges (employer/residence via hasRole), NOT as bare functional
# predicates — so dropping these would silently break their supersession. The
# registry's `functional` flag is the authoritative source for everything it
# names (spouse, appointment.organizer/location, …); is_functional unions the two
# until those role-edge concepts are modeled as functional registry predicates.
FUNCTIONAL_PREDICATES = frozenset(
    {"employer", "worksfor", "works_for", "spouse", "residence", "homelocation", "home_location"}
)


def is_functional(predicate: str) -> bool:
    """At most one current value, so a new binding supersedes. True if either the
    residual allowlist or the schema registry's `functional` flag says so."""
    from jbrain.schema import get_registry

    return predicate.lower() in FUNCTIONAL_PREDICATES or get_registry().is_functional(predicate)


# Reciprocity registry (docs/archive/research/fix-options/2-mutual-inverse-edges.md,
# Option 4a): which directed relationship edges the pipeline knows how to
# materialize on the OTHER party. Cardinality (FUNCTIONAL_PREDICATES) and
# reciprocity are orthogonal — spouse is both functional and symmetric — so
# these live beside, not inside, the functional set.
#
# Symmetric relations reflect with the SAME predicate (Celine.spouse -> Jeff).
# Both the schema.org spelling the prompt steers toward and its snake_case
# twin are listed, like FUNCTIONAL_PREDICATES, so a derived edge keyed on the
# lowercased predicate matches whichever spelling the model emitted.
SYMMETRIC_PREDICATES = frozenset(
    {
        "spouse",
        "married_to",
        "marriedto",
        "engaged_to",
        "engagedto",
        "sibling",
        "sibling_of",
        "siblingof",
        # Twins are siblings whose twin-ness rides the qualifier; a model that
        # reaches for a bare `twin` predicate instead still reciprocates.
        "twin",
        "twin_of",
        "twinof",
        "friend",
        "friend_of",
        "friendof",
        # A romantic/dating partner: the gender-neutral predicate the prompt now
        # steers toward, and the safe default the gendered boyfriend/girlfriend
        # pair can't be — partnership is mutual, so it reflects with itself for
        # any couple. A bare `partner` reads the same (business partners et al.
        # are partners too), so the symmetric reflection is always directionally
        # right even when the sense is non-romantic.
        "partner",
        "significant_other",
        "significantother",
        "cofounder",
        "co_founder",
        "business_partner",
        "businesspartner",
        "bandmate",
        "neighbor",
        "cousin",
    }
)

# Asymmetric relations reflect with a DIFFERENT, named inverse predicate. The
# map is symmetric in storage (both directions present) so a note may state
# either side; values are the canonical spelling the derived edge is written
# with. Lowercased keys; both schema.org and snake_case twins point at one
# canonical inverse spelling.
INVERSE_PAIRS: dict[str, str] = {
    "worksfor": "employs",
    "works_for": "employs",
    "employs": "worksFor",
    "parent_of": "child_of",
    "parentof": "child_of",
    "child_of": "parent_of",
    "childof": "parent_of",
    # Kinship the prompt steers toward (schema.org `parent` / `children`). The
    # parent's edge to their kid (Me.children -> Summer) reflects to the kid's
    # parent edge (Summer.parent -> Me); a bare `child` ("Summer is my child")
    # reads the same direction as `children`, so it reciprocates to `parent`
    # too. The `_of` pair above stays for the opposite-direction spelling.
    "parent": "children",
    "children": "parent",
    "child": "parent",
    "manages": "reportsTo",
    "reportsto": "manages",
    "reports_to": "manages",
    "tenant_of": "landlord_of",
    "tenantof": "landlord_of",
    "landlord_of": "tenant_of",
    "landlordof": "tenant_of",
    "mentor_of": "mentee_of",
    "mentorof": "mentee_of",
    "mentors": "mentee_of",
    "mentee_of": "mentor_of",
    "menteeof": "mentor_of",
    # Dating: the gendered words name an asymmetric pair (Summer.boyfriend ->
    # colin reflects to colin.girlfriend -> Summer). The mapping is the common
    # different-sex reading; a same-sex couple's reciprocal would be mis-gendered,
    # but the registry can't see the subjects' genders and the alternative —
    # leaving the bond one-directional — is the worse default the user hit.
    "boyfriend": "girlfriend",
    "girlfriend": "boyfriend",
    "hastreated": "treatedBy",
    "has_treated": "treatedBy",
    "treatedby": "hasTreated",
    "treated_by": "hasTreated",
    # Ownership: a possession edge (me.owns → the F-150) reciprocates to the
    # owner on the object's stream (the F-150 ownedBy → me). schema.org spells
    # both `owns`/`ownedBy`; the object is usually `Me` or a null-subject thing,
    # so the cross-subject gate rarely fires.
    "owns": "ownedBy",
    "ownedby": "owns",
    "owned_by": "owns",
    # Membership: a person's memberOf → Org reflects to the org's member list
    # (Org.member → person). Only the unambiguous person→org spelling reciprocates
    # — a bare `member` is directionally ambiguous (the prompt steers toward
    # memberOf), so it is intentionally absent and stands alone rather than
    # minting a wrong-way edge.
    "memberof": "member",
}


def inverse_predicate(predicate: str) -> str | None:
    """The predicate the reciprocal edge carries on the object entity, or None
    when this relation is not one we know how to reciprocate.

    Symmetric relations return the SAME predicate; asymmetric relations return
    their named inverse. An unknown predicate returns None — the directed edge
    stands alone, exactly as before. The registry is an allowlist, never a
    requirement: guessing an inverse for an unknown relation is the unsafe
    default, so we never do it.
    """
    key = predicate.lower()
    if key in SYMMETRIC_PREDICATES:
        return predicate
    return INVERSE_PAIRS.get(key)


# Schedule-binding predicates: an appointment's time is a binding whose value
# IS a validity instant, so ordering by validity would make a reschedule to an
# EARLIER time lose to the time it replaces. The newest INSTRUCTION wins
# regardless of direction (docs/reference/ANALYSIS.md "Temporal tokens and appointment
# identity"), so these order by reported_at. The set carries the schema.org
# spelling the prompt steers toward plus its snake_case twin, like
# FUNCTIONAL_PREDICATES above.
SCHEDULE_PREDICATES = frozenset({"scheduledtime", "scheduled_time"})


def is_schedule_binding(predicate: str) -> bool:
    return predicate.lower() in SCHEDULE_PREDICATES


# Below this, a candidate never auto-supersedes a MORE confident active fact;
# it parks in pending_review behind a low_confidence card instead.
# docs/reference/ANALYSIS.md "Guards" demands this for low-confidence numeric health
# facts (OCR-derived especially); we apply it domain-agnostically because
# decide() is deliberately pure (it never sees the fact's domain) and the
# asymmetry is safe — over-guarding a general fact costs one review card,
# under-guarding a health fact is a silent overwrite by garbage.
LOW_CONFIDENCE = 0.5

# Irrealis assertions are not claims about the present truth, so they must never
# auto-displace an ASSERTED current head — they park behind a conflict card for
# the owner. NEGATED is excluded (a negated disposal "I no longer own X" is a
# real retraction that supersedes); REPORTED (a third-party claim) and EXPECTED
# (a schedule binding / appointment) keep their shipped supersede semantics.
_IRREALIS = frozenset({"hypothetical", "question"})

# The "current floor" (Wave 1, slice 2) — the assertions that are a claim about
# the PRESENT truth and may therefore occupy an entity's current value slot. An
# `asserted` head is the live value; a `negated` head with no asserted peer is
# the live *retraction* state, shown explicitly as currently-negated (a real
# negation like "no longer allergic to penicillin" must not read as forgotten).
# The remaining modalities (hypothetical/reported/question/expected) are not
# claims about now, so they never floor as current. This is BROADER than
# _IRREALIS above: that set is only the two modalities barred from displacing an
# asserted head in supersession; here every non-asserted, non-negated modality
# is kept off the current floor. Used by the three read surfaces that previously
# had no assertion filter (entity_view, note_currency, canonical name); the
# graph/agent/consolidation paths already constrain `assertion = 'asserted'`.
CURRENT_ASSERTIONS = frozenset({"asserted", "negated"})


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
    # True when this existing row is itself a derived inverse. A derived
    # candidate may freely supersede another derived row (a shadow of its
    # source) but must never auto-overwrite a primary — that routes to review.
    derived: bool = False


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
    # `confidence` is the deterministic effective/plan weight (what the row
    # stores). `self_confidence` is the model's untrusted self-report, used only
    # by the low-confidence supersession guard — a surface-attested fact gets full
    # weight, but an uncertain READ of it must still not overwrite a confident
    # prior. Defaults to 1.0 (confident) for callers that don't carry a self-report.
    confidence: float = 1.0
    self_confidence: float = 1.0
    # True when this candidate comes from an owner-authored CORRECTION note. Such a
    # candidate "out-argues the graph": on a single-head address it force-supersedes
    # the current head(s) and commits active + pinned, regardless of temporal order
    # (the only path by which a human assertion forcibly overrides — Phase 6 §4).
    correction: bool = False
    # The incoming FHIR report status for an EMR lab reading (registered|preliminary|
    # final|amended|corrected|cancelled|entered-in-error). Set ONLY by the EMR importer
    # (docs/plans/EMR_IMPORT_PLAN.md §3.5); None for every other caller, whose
    # measurement path is then byte-for-byte unchanged. Drives `_lab_status_transition`,
    # which runs BEFORE the idempotency short-circuit so a same-value correction still
    # supersedes. Not persisted: the value fact's resulting lifecycle IS the record.
    fhir_status: str | None = None


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
    # The inserted row is pinned (an owner correction): protected from future
    # auto-supersession (a later non-correction note re-flags rather than flips it).
    insert_pinned: bool = False
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
    (docs/reference/ANALYSIS.md "Assertion status")."""
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
    "stays true about its interval" (docs/reference/ANALYSIS.md state row / SCD-2);
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


def _lab_status_transition(candidate: Candidate, existing: list[FactView]) -> Decision | None:
    """The status-aware measurement-supersession exception for EMR lab readings
    (docs/plans/EMR_IMPORT_PLAN.md §3.5). Inert for every existing caller — it
    returns None the instant the candidate is not a lab reading — so the default
    "measurements accumulate, never auto-supersede" policy is untouched.

    Runs BEFORE the idempotency short-circuit so a status-only correction
    (`final` -> `corrected`, value unchanged) still transitions instead of being
    swallowed as an identical-value refresh. Because "same draw ⇒ same qualifier ⇒
    same valid_from", the peer set is the heads at the candidate's valid_from, so a
    correction supersedes precisely the prior reading of THAT draw while a genuinely
    new draw (distinct valid_from) never enters the peer set and accumulates
    unchanged. Report status is NOT stored: this decision's RESULT (the value fact's
    lifecycle + supersession chain) is the durable record, from which §4.1 derives it.

    Idempotency on re-analysis (§6.6): every transition that changes a value leaves a
    durable marker at the draw — a `superseded` predecessor (corrected/final-promotion)
    or a `retracted` row (cancelled) — so a re-run detects the applied transition and
    refreshes in place instead of re-transitioning. A "correction" or "preliminary" with
    no reading to act on is deliberately NOT minted as a bare active value (which would be
    indistinguishable from a first correction on re-run, growing the chain): it is held in
    review or deferred to the unchanged path, both of which re-run idempotently.
    """
    if candidate.kind != "measurement" or candidate.fhir_status is None:
        return None
    status = candidate.fhir_status
    vf = candidate.valid_from
    peers = [e for e in existing if e.status in ("active", "pending_review") and e.valid_from == vf]
    active_peers = [e for e in peers if e.status == "active"]
    pending_peers = [e for e in peers if e.status == "pending_review"]

    if status in ("corrected", "amended"):
        # A revision supersedes the ACTIVE reading(s) of this draw — even when the value
        # is identical (the reason this runs before idempotency) — commits the correction
        # active, and holds any pending peer. Superseding an active leaves a `superseded`
        # predecessor, which is the durable marker that makes the re-run idempotent:
        #   - already applied (a superseded predecessor exists and the active head already
        #     equals the candidate) -> refresh that active head, do not re-transition.
        # When there is NO active reading to revise, a "correction" has no original: it is
        # anomalous and must NOT be minted as a citable active value (which would also be
        # indistinguishable from a plain final on re-run, growing the chain). It is held in
        # review instead — distinguishable (pending, not active) and idempotent. FHIR
        # `amended` collapses to `corrected` (§4.1).
        already = any(e.status == "superseded" and e.valid_from == vf for e in existing)
        same_active = [e for e in active_peers if values_equal(candidate, e)]
        diff_active = [e for e in active_peers if not values_equal(candidate, e)]
        if already and same_active and not diff_active:
            return Decision(refresh_id=same_active[0].id)
        if active_peers:
            return Decision(
                insert=True,
                insert_status="active",
                supersede_ids=[e.id for e in active_peers],
                hold_ids=[e.id for e in pending_peers],
            )
        same_pending = [e for e in pending_peers if values_equal(candidate, e)]
        if same_pending:
            return Decision(refresh_id=same_pending[0].id)  # re-run -> refresh the held row
        return Decision(
            insert=True,
            insert_status="pending_review",
            review_kind="low_confidence",
            review_extra={"subkind": "correction_without_original"},
        )
    if status == "preliminary":
        # Not yet a citable current value. If the draw is already FINALIZED (an active
        # reading exists) a late preliminary is stale — defer to the unchanged path
        # (refresh if equal, fact_conflict if it disagrees) rather than planting a
        # competing pending row that would false-flag the current value. Idempotent
        # re-run refreshes the pending row.
        if active_peers:
            return None
        if any(values_equal(candidate, e) for e in pending_peers):
            return None
        return Decision(insert=True, insert_status="pending_review")
    if status == "final":
        # An active reading of this draw already exists -> defer to the unchanged path
        # (idempotent refresh if equal, same-instant fact_conflict if it disagrees); never
        # insert a second active beside it. Otherwise finalization promotes a preliminary:
        # supersede the pending reading, commit active. (On re-run the promoted preliminary
        # is superseded, not pending, so pending_peers is empty and the refresh path runs.)
        if active_peers:
            return None
        if pending_peers:
            return Decision(
                insert=True,
                insert_status="active",
                supersede_ids=[e.id for e in pending_peers],
            )
        return None
    if status in ("cancelled", "entered-in-error"):
        # The reading is withdrawn: insert a retracted row (dropped by the projection,
        # §4.1) and supersede the prior active. Re-run refreshes the retracted row
        # (which `decide`'s `live` filter excludes, so returning None would wrongly
        # re-insert an active reading).
        retracted = next(
            (
                e
                for e in existing
                if e.status == "retracted" and e.valid_from == vf and values_equal(candidate, e)
            ),
            None,
        )
        if retracted is not None:
            return Decision(refresh_id=retracted.id)
        return Decision(
            insert=True,
            insert_status="retracted",
            supersede_ids=[e.id for e in active_peers],
            hold_ids=[e.id for e in pending_peers],
        )
    # `registered` (dormant; no such data in the corpus) and any unknown status make
    # no transition — the reading flows through the unchanged measurement path.
    return None


def decide(candidate: Candidate, existing: list[FactView], *, predicate: str = "") -> Decision:
    """Resolve one candidate against the identity key's existing facts."""
    live = [e for e in existing if e.status != "retracted"]

    closed = _interval_close(candidate, live, predicate)
    if closed is not None:
        return closed

    # The EMR status-aware transition runs BEFORE the idempotency short-circuit
    # (§3.5) so a same-value correction still supersedes. Inert (returns None) for
    # every non-lab caller — `fhir_status` is None everywhere but the EMR importer.
    lab = _lab_status_transition(candidate, existing)
    if lab is not None:
        return lab

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
        # A now-OPEN restatement of a CLOSED interval is a RE-OPEN (rejoined a
        # former employer), not an idempotent refresh — refresh writes only
        # rendering and would strand the row closed. Fall through to insert a
        # fresh open interval beside the closed history.
        if candidate.valid_to is None and e.valid_to is not None:
            continue
        same_validity = e.valid_from == candidate.valid_from
        if accumulating and same_validity:
            return Decision(refresh_id=e.id)
        if not accumulating and (e.status in ("active", "pending_review") or same_validity):
            return Decision(refresh_id=e.id)

    # An owner correction out-argues the graph on a SINGLE-HEAD address (state /
    # attribute / preference / functional relationship): supersede every current head
    # and commit the correction active + pinned, regardless of temporal order. An
    # identical-value restatement was already refreshed above, so reaching here means a
    # genuine override. Accumulating kinds (event/measurement) and set-valued
    # (non-functional) relationships fall through to the normal path — a correction
    # there is a new datapoint/edge, not a wholesale replacement.
    single_head = candidate.kind in ("state", "attribute", "preference") or (
        candidate.kind == "relationship" and is_functional(predicate)
    )
    if candidate.correction and single_head:
        heads = [e for e in live if e.status in ("active", "pending_review")]
        return Decision(
            insert=True,
            insert_status="active",
            insert_pinned=True,
            supersede_ids=[e.id for e in heads if e.status == "active"],
            hold_ids=[e.id for e in heads if e.status == "pending_review"],
        )

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
        # Set-valued: edges to DISTINCT objects are co-equal live values and
        # accumulate. But an ASSERTED and a NEGATED edge to the SAME object are a
        # contradiction ("X is my friend" vs the unfriend "X is not my friend") —
        # there is no `value`, so the edge identity IS the object_entity_id. An
        # equal-polarity restatement was already taken by the refresh path above,
        # so a remaining opposite-polarity same-object peer is the contradiction:
        # hold the candidate behind a fact_conflict card (keyed on the peer's id —
        # re-ingest refreshes both rows in place via the path above, so the card
        # is never re-filed) rather than letting two opposite-polarity edges sit
        # live. Other modality mixes (a "maybe" friend beside an asserted one)
        # aren't contradictions and still accumulate.
        contradiction = next(
            (
                e
                for e in live
                if candidate.object_entity_id is not None
                and e.object_entity_id == candidate.object_entity_id
                and e.status in ("active", "pending_review")
                and e.assertion != candidate.assertion
                and e.assertion in CURRENT_ASSERTIONS
                and candidate.assertion in CURRENT_ASSERTIONS
            ),
            None,
        )
        if contradiction is not None:
            return Decision(
                insert=True,
                insert_status="pending_review",
                review_kind="fact_conflict",
                conflicting_id=contradiction.id,
            )
        return Decision(insert=True)  # non-functional edges accumulate

    # state / preference / functional relationship: at most one CURRENT (OPEN)
    # value; closed intervals are history that never contend for it.
    actives = [e for e in live if e.status == "active"]
    open_actives = [e for e in actives if e.valid_to is None]

    def key(valid_from: datetime | None, reported_at: datetime) -> tuple[datetime, datetime]:
        # Preferences are valid from when reported — newest report wins.
        # Schedule bindings also order by reported_at: the latest reschedule
        # instruction wins even when it moves the time earlier.
        if candidate.kind == "preference" or is_schedule_binding(predicate):
            return (reported_at, reported_at)
        return _validity(valid_from, reported_at)

    # Closed-on-arrival ("used to work for X"): history, never the CURRENT head
    # (docs/archive/research/legacy-links-handling.md §3.2). Gated to ASSERTED
    # state/relationship: a NEGATED disposal supersedes (falls through), and an
    # open-vs-open close was already taken by _interval_close.
    if (
        candidate.valid_to is not None
        and candidate.assertion == "asserted"
        and candidate.kind in ("state", "relationship")
    ):
        if open_actives:
            # An open value holds the current slot — this past value lands behind
            # it as closed retrospective history (chained), never displacing the
            # open value the way a same-note reported_at tie would.
            current_open = max(open_actives, key=lambda e: key(e.valid_from, e.reported_at))
            return Decision(
                insert=True,
                insert_status="superseded",
                insert_superseded_by=current_open.id,
                insert_valid_to=candidate.valid_to,
            )
        # No open current. A same-INTERVAL, same-object, different-VALUE closed
        # peer is a CORRECTION of that historical value ("in 2019 it was Columbus,
        # not Cleveland") — newest-report wins, supersede + conflict. Otherwise the
        # value is PARALLEL history — a co-equal closed head (two unrelated past
        # jobs, or a distinct interval): no supersede, no conflict.
        correction = max(
            (
                e
                for e in actives
                if e.valid_to is not None
                and e.valid_from == candidate.valid_from
                and e.object_entity_id == candidate.object_entity_id
                and not values_equal(candidate, e)
            ),
            key=lambda e: key(e.valid_from, e.reported_at),
            default=None,
        )
        if correction is None:
            return Decision(insert=True, insert_valid_to=candidate.valid_to)
        if correction.pinned:
            # Re-flag a pinned human decision, never auto-flip it.
            return Decision(
                insert=True,
                insert_valid_to=candidate.valid_to,
                insert_status="pending_review",
                review_kind="fact_conflict",
                conflicting_id=correction.id,
            )
        if (
            candidate.self_confidence < LOW_CONFIDENCE
            and candidate.self_confidence < correction.confidence
        ):
            return Decision(
                insert=True,
                insert_valid_to=candidate.valid_to,
                insert_status="pending_review",
                review_kind="low_confidence",
                conflicting_id=correction.id,
            )
        return Decision(
            insert=True,
            insert_valid_to=candidate.valid_to,
            supersede_ids=[correction.id],
            review_kind="fact_conflict",
            conflicting_id=correction.id,
        )

    if not open_actives:
        return Decision(insert=True)

    current = max(open_actives, key=lambda e: key(e.valid_from, e.reported_at))
    if key(candidate.valid_from, candidate.reported_at) >= key(
        current.valid_from, current.reported_at
    ):
        # An irrealis candidate (hypothetical/question) is not a claim about the
        # present, so it must not displace an ASSERTED current head: park it
        # behind a conflict card and leave the asserted value active (mirrors the
        # pinned guard below). A negated/reported/expected candidate keeps its
        # shipped semantics and falls through.
        if candidate.assertion in _IRREALIS and current.assertion == "asserted":
            return Decision(
                insert=True,
                insert_status="pending_review",
                review_kind="fact_conflict",
                conflicting_id=current.id,
            )
        if current.pinned:
            # Re-flag, never flip: the human decision survives reprocessing.
            return Decision(
                insert=True,
                insert_status="pending_review",
                review_kind="fact_conflict",
                conflicting_id=current.id,
            )
        if (
            candidate.self_confidence < LOW_CONFIDENCE
            and candidate.self_confidence < current.confidence
        ):
            # A blurry OCR read must not silently overwrite confident
            # knowledge: park the candidate, keep the current fact active,
            # and let a human adjudicate via the low_confidence card. Keyed on
            # the model's self-report (not the plan weight a surface-attested
            # fact carries), so the guard survives the integrate weight model.
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
