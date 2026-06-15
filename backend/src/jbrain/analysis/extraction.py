"""Parsing and validation of the note.extract JSON payload.

Strict on structure (a payload that isn't the documented shape is a permanent
job failure — the adapter already spent its one re-ask), lenient on individual
items: a single fact with a bogus enum is dropped and logged rather than
sinking the whole note.
"""

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from jbrain.analysis.prompt import MAX_FACTS
from jbrain.analysis.supersession import _same_quantity
from jbrain.schema import get_registry

log = structlog.get_logger()

FACT_KINDS = frozenset({"event", "measurement", "state", "attribute", "preference", "relationship"})
ASSERTIONS = frozenset({"asserted", "negated", "hypothetical", "reported", "question", "expected"})
PRECISIONS = frozenset({"instant", "day", "month", "year", "era", "unknown"})
TOKEN_KINDS = frozenset({"point", "range", "recurrence"})
DOMAINS = frozenset({"general", "health", "finance", "location"})
RESTRICTED_DOMAINS = frozenset({"health", "finance", "location"})

MAX_TAGS = 6

# Server-side teeth for the prompt's instructions (red-team H1): a hostile or
# runaway extraction must not write unbounded rows or unbounded row WIDTH.
# predicate/qualifier are identity-key parts — truncating one could collide
# two distinct keys, so an oversized key REJECTS the fact. A statement is
# rendering only, so it truncates harmlessly. value_json is opaque jsonb a
# real personal-data payload never approaches 16 KiB of, so an oversized one
# is dropped (the fact survives on its statement). Limits are generous on
# purpose: they exist to stop abuse, not to second-guess the prompt.
MAX_KEY_CHARS = 200
MAX_STATEMENT_CHARS = 1000
MAX_VALUE_JSON_BYTES = 16384


class ExtractionError(Exception):
    """The payload does not have the documented top-level shape."""


@dataclass(frozen=True)
class ExtractedTemporal:
    phrase: str | None
    resolved_start: datetime | None
    resolved_end: datetime | None
    precision: str


@dataclass(frozen=True)
class ExtractedMention:
    name: str
    kind: str
    surface_text: str


@dataclass(frozen=True)
class ExtractedToken:
    phrase: str
    kind: str
    resolved_start: datetime
    resolved_end: datetime | None
    precision: str
    rrule: str | None


@dataclass(frozen=True)
class ExtractedFact:
    predicate: str
    qualifier: str
    kind: str
    statement: str
    value_json: dict[str, Any] | None
    assertion: str
    entity_ref: str
    object_entity_ref: str | None
    temporal: ExtractedTemporal | None
    domain: str
    confidence: float
    # The model's untrusted self-report, carried separately from `confidence`
    # (which integrate sets to the deterministic plan weight). Used ONLY by the
    # supersession low-confidence guard, so a surface-attested-but-uncertain read
    # — full plan weight — still can't silently overwrite a confident prior.
    # Defaults to 1.0 so non-integrate constructions are treated as confident.
    self_confidence: float = 1.0


@dataclass(frozen=True)
class Extraction:
    title: str
    tags: list[str]
    mentions: list[ExtractedMention]
    facts: list[ExtractedFact]
    tokens: list[ExtractedToken]
    # How many facts the per-note budget dropped from the tail (0 = none). The
    # pipeline surfaces a non-zero count as a review card so a truncated long
    # note is visible, not silently clipped.
    dropped_facts: int = 0


def parse_datetime(value: Any) -> datetime | None:
    """ISO 8601 -> aware datetime; naive values are pinned to UTC (the anchor
    in the prompt carries the real offset, so naive output is model slop, not
    a different timezone). None/unparseable -> None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# Deterministic domain FLOOR by predicate (firewall hardening): a fact on a
# clearly-sensitive predicate is AT LEAST its domain regardless of the model's
# per-fact judgment, closing the leak path where a clinical/financial fact gets
# mislabeled `general`. Ratchet-UP only (general -> restricted, never down or
# across restricted), matching the asymmetric bias in docs/ANALYSIS.md
# ("misclassifying into health/finance is cheap; out of it is a leak"). A curated
# allowlist of canonical schema.org/LOINC-ish predicates, lowercased; unknown
# predicates fall back to the model (which already classifies well). A full LOINC
# table is the Phase-7 typed-record job; weight/temperature are deliberately
# OUT as too ambiguous to floor.
_DOMAIN_BY_PREDICATE: dict[str, str] = {
    **{
        p: "health"
        for p in (
            "bloodpressure", "bloodglucose", "fastingglucose", "hemoglobina1c", "a1c",
            "ldlcholesterol", "ldl", "hdl", "cholesterol", "triglycerides", "troponin",
            "inr", "tsh", "heartrate", "restingheartrate", "oxygensaturation", "o2sat",
            "respiratoryrate", "medication", "medicationregimen", "takesmedication",
            "prescribes", "diagnosis", "medicalcondition", "healthcondition", "allergy",
            "immunization", "vaccination",
        )
    },
    **{
        p: "finance"
        for p in (
            "accountbalance", "mortgagebalance", "mortgage", "interestrate", "refinancerate",
            "retirementcontribution", "accountcontribution", "hasaccount", "brokerageaccount",
        )
    },
    # Only PRECISE geo is location-firewall-sensitive; a home city (homeLocation/
    # residence) is ordinary and left to the model + ratchet.
    **{p: "location" for p in ("geocoordinates", "latitude", "longitude", "gpscoordinates")},
}  # fmt: skip


def domain_floor(predicate: str) -> str | None:
    """The minimum (restricted) domain a clearly-sensitive predicate forces, or
    None for predicates the model is left to classify on its own."""
    return _DOMAIN_BY_PREDICATE.get(predicate.lower())


def ratchet_domain(extracted: str, note_domain: str) -> tuple[str, bool]:
    """Apply the asymmetric domain bias (docs/ANALYSIS.md "Domains").

    Returns (fact_domain, needs_promotion_review): ratcheting INTO a
    restricted domain from a general note is free; anything that would make a
    fact less restricted than its note (or move it across restricted domains)
    keeps the note's domain and proposes the change via the review inbox.
    """
    if extracted == note_domain:
        return extracted, False
    if note_domain == "general" and extracted in RESTRICTED_DOMAINS:
        return extracted, False
    return note_domain, True


def normalize_future_assertion(fact: ExtractedFact, anchor: datetime) -> ExtractedFact:
    """A fact whose validity is still in the future has not occurred yet, so it
    cannot be an asserted past `event` (docs/ANALYSIS.md "Temporal model":
    future-tense facts carry `expected`). The v2 prompt teaches this, but a
    model lapse must not land a follow-up "in 3 months" as a bare asserted
    event — that would read as something that already happened. This is a
    temporal-correctness rule, not a kind rule: we only relax assertion, never
    rewrite the model's chosen kind.
    """
    start = fact.temporal.resolved_start if fact.temporal else None
    if start is not None and start > anchor and fact.assertion == "asserted":
        return replace(fact, assertion="expected")
    return fact


# Backward-looking companion to normalize_future_assertion. The model resolves
# every relative phrase against the capture anchor (prompt.py temporal rule),
# but a common lapse is an off-by-one on BACKWARD phrases: "last night" captured
# at 07:13 lands on the capture day instead of the prior evening (owner field
# report, Jun 2026). For a CLOSED set of phrases whose correct LOCAL calendar
# date is unambiguous given the anchor, we recompute deterministically and
# repair a wrong one. Novel or genuinely ambiguous phrases ("last week", "around
# the holidays") are left to the model — we only override where we are certain.
# The anchor is the capture time in the note's local offset (pipeline.
# local_anchor); that offset is fixed, so timedelta-day arithmetic preserves the
# local wall clock and stays DST-safe (the hist_dst_boundary lesson).

_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}  # fmt: skip
# Phrases denoting the anchor's OWN local day vs the immediately prior day.
_SAME_DAY = frozenset({"today", "tonight", "this morning", "this afternoon", "this evening"})
_PRIOR_DAY = frozenset(
    {"yesterday", "yesterday morning", "yesterday afternoon", "yesterday evening"}
)
# "last night" pins no calendar day by itself: from a daytime anchor it is
# unambiguously the prior evening, but from a late-evening anchor it can mean
# earlier the same night. Only repair it from a daytime capture; otherwise leave
# the model's reading alone (the bug we fix is the 07:13 morning case).
_LAST_NIGHT_DAYTIME_CUTOFF = 18


def _normalize_phrase(phrase: str) -> str:
    return re.sub(r"\s+", " ", phrase.strip().lower()).strip(" .,!?;:\"'")


def _word_or_int(token: str) -> int | None:
    return int(token) if token.isdigit() else _WORD_NUMBERS.get(token)


def resolve_relative_date(phrase: str | None, anchor: datetime) -> date | None:
    """The LOCAL calendar date a backward relative phrase denotes, or None when
    the phrase is outside the closed deterministic set (left to the model)."""
    if not phrase:
        return None
    p = _normalize_phrase(phrase)
    base = anchor.date()
    if p in _SAME_DAY:
        return base
    if p in _PRIOR_DAY:
        return base - timedelta(days=1)
    if p == "last night":
        return base - timedelta(days=1) if anchor.hour < _LAST_NIGHT_DAYTIME_CUTOFF else None
    if p in ("day before yesterday", "the day before yesterday"):
        return base - timedelta(days=2)
    if m := re.fullmatch(r"(\w+) days? ago", p):
        n = _word_or_int(m.group(1))
        return base - timedelta(days=n) if n is not None else None
    if m := re.fullmatch(r"(\w+) weeks? ago", p):
        n = _word_or_int(m.group(1))
        return base - timedelta(weeks=n) if n is not None else None
    if (m := re.fullmatch(r"last (\w+)", p)) and m.group(1) in _WEEKDAYS:
        # The PRIOR occurrence, strictly before the anchor's own weekday.
        delta = (base.weekday() - _WEEKDAYS[m.group(1)]) % 7 or 7
        return base - timedelta(days=delta)
    return None


def _repair_dates(
    phrase: str | None,
    start: datetime | None,
    end: datetime | None,
    anchor: datetime,
) -> tuple[datetime | None, datetime | None, bool]:
    """Shift a mis-resolved backward phrase onto its correct calendar date,
    preserving time-of-day, offset, and any range width. Returns
    (start, end, repaired); a phrase outside the closed set is a no-op."""
    expected = resolve_relative_date(phrase, anchor)
    if start is None or expected is None:
        return start, end, False
    # Judge the model's instant by the calendar day it falls on IN THE NOTE'S
    # LOCAL timezone (the anchor's offset), not by a raw .date() that depends on
    # whichever offset the model happened to emit. grok routinely resolves to a
    # UTC instant rather than echoing the local offset, so an exact-offset check
    # would skip the repair in exactly the case it exists for. The pipeline only
    # supplies an anchor when the client offset is known, so anchor.tzinfo is
    # always a real local offset here (fixed, hence DST-safe day arithmetic).
    local_start_date = start.astimezone(anchor.tzinfo).date()
    if expected == local_start_date:
        return start, end, False
    delta = timedelta(days=(expected - local_start_date).days)
    return start + delta, (end + delta if end is not None else None), True


# Precisions whose value is a CALENDAR DATE, not an instant — stamped to local
# midnight so the date reads correctly in the note's timezone (see
# _stamp_local_midnight). instant/era/unknown keep their resolved value.
_DATE_PRECISIONS = frozenset({"day", "month", "year"})


def _stamp_local_midnight(dt: datetime, anchor: datetime) -> datetime:
    """Local midnight on the date the model WROTE, in the note's offset."""
    d = dt.date()
    return datetime(d.year, d.month, d.day, tzinfo=anchor.tzinfo)


def _drifted_utc_midnight(dt: datetime, anchor: datetime) -> bool:
    """The midnight-UTC date bug: the model rendered a bare calendar date as
    midnight UTC ("June 8" -> 2026-06-08T00:00Z), which in a western offset is
    the PRIOR evening, so the local date reads one day early. The signature is
    exactly UTC midnight whose local date differs from the written date — a
    real evening instant (a correctly-resolved "last night" at 02:00Z) is NOT
    midnight and is left alone (eval baseline, grok-4.3 Jun 2026)."""
    return (
        dt.utcoffset() == timedelta(0)
        and (dt.hour, dt.minute, dt.second) == (0, 0, 0)
        and dt.astimezone(anchor.tzinfo).date() != dt.date()
    )


# Part-of-day windows (local hours) for time-of-day phrases — a deterministic
# enrichment so "evening" / "last night" / "this morning" carry their within-day
# meaning instead of collapsing to a bare date. midnight is left alone (a point,
# and "night" must not swallow it).
def _part_of_day_window(phrase: str | None) -> tuple[int, int] | None:
    if not phrase:
        return None
    p = phrase.lower()
    if "afternoon" in p:
        return (12, 18)
    if "morning" in p:
        return (6, 12)
    if "evening" in p:
        return (18, 23)
    if "night" in p and "midnight" not in p:  # tonight / last night / overnight
        return (18, 23)
    return None


def _local_time_on(start: datetime, hour: int, anchor: datetime) -> datetime:
    """`hour`:00 local, on the calendar date `start` falls on in the note's tz."""
    d = start.astimezone(anchor.tzinfo).date()
    return datetime(d.year, d.month, d.day, hour, tzinfo=anchor.tzinfo)


def finalize_temporal(
    phrase: str | None,
    start: datetime | None,
    end: datetime | None,
    precision: str,
    anchor: datetime,
) -> tuple[datetime | None, datetime | None, bool]:
    """Repair a mis-resolved backward phrase and fix a date-precision value the
    model rendered as drifted midnight-UTC. Returns (start, end, changed).

    Part-of-day RANGE enrichment is applied only to TOKENS (the token loop), not
    here: a fact's valid_from must not gain an hour offset that would reorder
    same-day supersession, and a fact must never gain a valid_to (it would
    falsely close a `state` interval). Tokens are the first-class range objects
    (docs/ANALYSIS.md), so the within-day meaning lives there."""
    start, end, changed = _repair_dates(phrase, start, end, anchor)
    # Midnight-UTC normalization is for ABSOLUTE dates only ("June 8"). A KNOWN
    # relative phrase ("today"/"yesterday") is already handled by _repair_dates,
    # whose value is locally correct even when rendered at midnight UTC; stamping
    # it to the written (UTC) date would PUSH it a day the wrong way (e.g.
    # "yesterday" -> 2026-06-11T00:00Z is locally Jun 10, must not become Jun 11).
    if precision in _DATE_PRECISIONS and resolve_relative_date(phrase, anchor) is None:
        if start is not None and _drifted_utc_midnight(start, anchor):
            start, changed = _stamp_local_midnight(start, anchor), True
        if end is not None and _drifted_utc_midnight(end, anchor):
            end, changed = _stamp_local_midnight(end, anchor), True
    return start, end, changed


def validate_backward_temporal(
    temporal: ExtractedTemporal | None, anchor: datetime
) -> tuple[ExtractedTemporal | None, bool]:
    """Repair a backward relative phrase the model resolved to the wrong day and
    stamp date-precision values to local midnight. Returns (temporal, changed)."""
    if temporal is None:
        return None, False
    start, end, changed = finalize_temporal(
        temporal.phrase, temporal.resolved_start, temporal.resolved_end, temporal.precision, anchor
    )
    if not changed:
        return temporal, False
    return replace(temporal, resolved_start=start, resolved_end=end), True


def _parse_temporal(raw: Any) -> ExtractedTemporal | None:
    if not isinstance(raw, dict):
        return None
    precision = raw.get("precision")
    start = parse_datetime(raw.get("resolved_start"))
    phrase = raw.get("phrase")
    return ExtractedTemporal(
        phrase=str(phrase) if phrase else None,
        resolved_start=start,
        resolved_end=parse_datetime(raw.get("resolved_end")),
        precision=precision if precision in PRECISIONS else "unknown",
    )


def _clamp_confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


# Same-key dedup (prompt v4's "ONE fact per entity+predicate per note", with
# server-side teeth): a model that restates one property across renderings,
# units, or kinds must yield ONE stored fact, not an attribute collision the
# owner resolves by hand. The line we draw: a DUPLICATE (same value re-rendered,
# unit-converted, re-kinded, or the same date at differing precision) collapses
# silently; a CONTRADICTION (genuinely different values on the same key, e.g.
# adv_self_contradiction_one_note's Reykjavik-then-Oslo) keeps both facts so
# the supersession/conflict machinery — the part that works — decides.

_PRECISION_RANK = {"year": 1, "month": 2, "day": 3, "instant": 4}


def _calendar_key(start: datetime, precision: str) -> tuple[int, ...]:
    # Calendar components as stamped (no astimezone): day/month/year temporals
    # are calendar dates anchored by the extractor, not comparable instants.
    parts = (start.year, start.month, start.day, start.hour, start.minute, start.second)
    return parts[: _PRECISION_RANK[precision] if precision != "instant" else 6]


def temporals_consistent(a: ExtractedTemporal | None, b: ExtractedTemporal | None) -> bool:
    """Can these two temporal claims describe the same moment?

    A missing side is vacuously consistent (no time claim to contradict).
    Identical claims are consistent. Otherwise both calendar values must agree
    once truncated to the VAGUER precision — "March 1986" contains
    "March 19, 1986" but not "April 4, 1986". era/unknown precisions are never
    assumed consistent: there is nothing sound to truncate to.
    """
    if a is None or b is None or a.resolved_start is None or b.resolved_start is None:
        return True
    if a.precision == b.precision and a.resolved_start == b.resolved_start:
        return True
    rank_a, rank_b = _PRECISION_RANK.get(a.precision), _PRECISION_RANK.get(b.precision)
    if rank_a is None or rank_b is None:
        return False
    vaguer = a.precision if rank_a <= rank_b else b.precision
    return _calendar_key(a.resolved_start, vaguer) == _calendar_key(b.resolved_start, vaguer)


def _values_agree(vague: dict[str, Any] | None, precise: dict[str, Any] | None) -> bool:
    """Same value modulo rendering: equal payloads, a unit-converted quantity
    (76 in vs 193 cm), or one side carrying no payload at all (a valueless
    restatement rides its statement)."""
    if vague is None or precise is None or vague == precise:
        return True
    return _same_quantity(vague, precise)


def _date_prefix_agrees(vague: dict[str, Any], precise: dict[str, Any]) -> bool:
    # Differing-precision renderings of one date ("1986-03" vs "1986-03-19"):
    # every shared string field of the vaguer payload must be a prefix of the
    # preciser's. Only consulted when temporal precisions differ, so same-rank
    # near-strings ("York" vs "Yorkshire") can never collapse through here.
    shared = set(vague) & set(precise)
    return bool(shared) and all(
        isinstance(vague[k], str)
        and isinstance(precise[k], str)
        and precise[k].startswith(vague[k])
        for k in shared
    )


def _duplicate_winner(a: ExtractedFact, b: ExtractedFact) -> ExtractedFact | None:
    """The surviving fact when `b` (later in the array) restates `a`'s
    property; None means they genuinely differ — a contradiction for the
    supersession machinery, never collapsed here.

    Winner rule, in order:
    1. Both sides dated at DIFFERENT precisions describing the same consistent
       date: the more precise fact wins regardless of confidence — extra
       information beats a confidence score on a vaguer rendering.
    2. Otherwise highest confidence; tie → the side carrying a value_json
       payload; still tied → later in the array, matching the pipeline's
       intra-note last-wins convention for same-key facts.
    """
    if not temporals_consistent(a.temporal, b.temporal):
        return None
    rank_a = _PRECISION_RANK.get(a.temporal.precision) if a.temporal else None
    rank_b = _PRECISION_RANK.get(b.temporal.precision) if b.temporal else None
    if rank_a is not None and rank_b is not None and rank_a != rank_b:
        vague, precise = (a, b) if rank_a < rank_b else (b, a)
        agree = _values_agree(vague.value_json, precise.value_json) or (
            vague.value_json is not None
            and precise.value_json is not None
            and _date_prefix_agrees(vague.value_json, precise.value_json)
        )
        return precise if agree else None
    if not _values_agree(a.value_json, b.value_json):
        return None
    if a.confidence != b.confidence:
        return a if a.confidence > b.confidence else b
    if (a.value_json is None) != (b.value_json is None):
        return a if a.value_json is not None else b
    return b


def dedup_facts(facts: list[ExtractedFact]) -> list[ExtractedFact]:
    """Collapse same-key duplicates within ONE extraction, keeping the best
    fact per _duplicate_winner. The key includes object_entity_ref (distinct
    edges like Me.owns→Bella vs Me.owns→Ricky are different facts) and
    assertion (a negation is never a restatement of the assertion it negates).
    """

    def key(f: ExtractedFact) -> tuple[str, str, str, str | None, str]:
        return (f.entity_ref, f.predicate, f.qualifier, f.object_entity_ref, f.assertion)

    kept: list[ExtractedFact] = []
    for fact in facts:
        merged = False
        for i, prior in enumerate(kept):
            if key(prior) != key(fact):
                continue
            winner = _duplicate_winner(prior, fact)
            if winner is None:
                continue  # genuinely different values: both stay
            dropped = fact if winner is prior else prior
            log.warning(
                "analysis.fact_deduped",
                predicate=fact.predicate,
                entity=fact.entity_ref,
                kept_kind=winner.kind,
                dropped_kind=dropped.kind,
                dropped_statement=dropped.statement[:80],
            )
            kept[i] = winner
            merged = True
            break
        if not merged:
            kept.append(fact)
    return kept


# A trailing possessive ("Celine's" -> "Celine") and case are the usual gap
# between an object_entity_ref and the mention it means; normalize both sides
# before matching so a near-miss links instead of orphaning the edge.
_POSSESSIVE = re.compile(r"['’]s?$")


def _norm_ref(text: str) -> str:
    return _POSSESSIVE.sub("", text.strip().lower())


def link_relationship_objects(
    facts: list[ExtractedFact], mentions: list[ExtractedMention]
) -> list[ExtractedFact]:
    """Deterministically bind a fact's object_entity_ref to a mention.

    A near-miss ref snaps to its mention for ANY object edge; a DROPPED ref is
    recovered only for `relationship` facts (a state fact renders its value_json
    instead, so it has no display gap to justify the inference risk).

    The model is the weak link (docs/research/fix-options/1): grok sets
    object_entity_ref inconsistently — sometimes naming the object, sometimes
    folding it into the statement, sometimes near-missing the mention's name.
    That run-to-run flip swings a relationship edge between linked and unlinked,
    so the property renders its whole statement sentence instead of the object
    entity's name (the `spouse -> "I have a wife Celine Hopkins."` report). This
    net makes the binding a pure function of (mentions, fact): a near-miss ref
    snaps to its mention; a dropped ref is recovered from value_json or the one
    non-subject mention the statement names. The object only ever binds to a
    mention the model ALREADY emitted — never a minted entity — so it cannot
    hallucinate a person (the risk that ruled out auto-minting, Option B2a).
    """
    if not mentions:
        return facts
    by_norm: dict[str, str] = {}
    for m in mentions:
        for surface in (m.name, m.surface_text):
            by_norm.setdefault(_norm_ref(surface), m.name)

    linked: list[ExtractedFact] = []
    for fact in facts:
        ref = fact.object_entity_ref
        if ref:
            # Snap a near-miss to the mention it means. Safe for ANY object edge
            # (a relationship, or a state like worksFor/homeLocation): it only
            # re-points an emitted ref at an emitted mention, never invents one.
            if not any(m.name == ref for m in mentions):
                snapped = by_norm.get(_norm_ref(ref))
                if snapped is not None and snapped != ref:
                    log.info(
                        "analysis.object_ref_snapped",
                        predicate=fact.predicate,
                        was=ref,
                        mention=snapped,
                    )
                    fact = replace(fact, object_entity_ref=snapped)
        elif fact.kind == "relationship":
            # Recovery (inferring a DROPPED object) is bounded to relationship
            # facts: a state fact already renders its value_json place/value, so
            # there is no display gap to justify the inference's risk.
            recovered = _recover_object_ref(fact, mentions, by_norm)
            if recovered is not None:
                log.info(
                    "analysis.object_ref_recovered", predicate=fact.predicate, mention=recovered
                )
                fact = replace(fact, object_entity_ref=recovered)
        linked.append(fact)
    return linked


def _recover_object_ref(
    fact: ExtractedFact, mentions: list[ExtractedMention], by_norm: dict[str, str]
) -> str | None:
    """Recover a dropped object_entity_ref from the relationship fact's payload.

    Two deterministic signals, in order: a single-datum value_json the model
    sometimes folds the object into ({"value"|"name"|"place": X}), then the
    mentions whose surface the STATEMENT names verbatim. Either must resolve to
    exactly ONE non-subject mention to bind — ambiguity or no match leaves the
    edge unlinked rather than guess the wrong person.
    """
    if isinstance(fact.value_json, dict):
        for key in ("value", "name", "place"):
            raw = fact.value_json.get(key)
            if isinstance(raw, str):
                hit = by_norm.get(_norm_ref(raw))
                if hit is not None and hit != fact.entity_ref:
                    return hit

    statement = fact.statement.lower()
    named = set()
    for m in mentions:
        if m.name == fact.entity_ref:
            continue
        norm = _norm_ref(m.surface_text)
        if norm and re.search(rf"\b{re.escape(norm)}\b", statement):
            named.add(m.name)
    return next(iter(named)) if len(named) == 1 else None


def parse_extraction(
    payload: Any, *, anchor: datetime | None = None, max_facts: int = MAX_FACTS
) -> Extraction:
    """Validate the parsed JSON into typed extraction objects.

    When `anchor` (the capture time in the note's local offset) is given,
    backward relative phrases the model mis-resolved are repaired against it
    (validate_backward_temporal); callers that don't care about temporal
    correctness — most unit tests — omit it and get the raw resolution.

    `max_facts` is the per-note cap (the pipeline passes fact_cap(note); callers
    that omit it get the hard ceiling). It enforces the same budget the user
    prompt advertised, so a model that over-extracts is trimmed to the tail.

    Raises:
        ExtractionError: the top-level shape is wrong (permanent failure).
    """
    if not isinstance(payload, dict):
        raise ExtractionError("extraction payload is not a JSON object")
    for key, typ in (("title", str), ("tags", list), ("mentions", list), ("facts", list)):
        if not isinstance(payload.get(key), typ):
            raise ExtractionError(f"extraction missing or mistyped field: {key!r}")

    tags: list[str] = []
    for tag in payload["tags"]:
        cleaned = str(tag).strip().lower()
        if cleaned and cleaned not in tags:
            tags.append(cleaned)

    mentions: list[ExtractedMention] = []
    for raw in payload["mentions"]:
        if not isinstance(raw, dict) or not str(raw.get("name", "")).strip():
            log.warning("analysis.mention_dropped", reason="malformed", raw_type=type(raw).__name__)
            continue
        mentions.append(
            ExtractedMention(
                name=str(raw["name"]).strip(),
                kind=str(raw.get("kind") or "Thing").strip() or "Thing",
                surface_text=str(raw.get("surface_text") or raw["name"]),
            )
        )

    facts: list[ExtractedFact] = []
    for raw in payload["facts"]:
        if not isinstance(raw, dict):
            log.warning("analysis.fact_dropped", reason="not an object")
            continue
        kind, assertion = raw.get("kind"), raw.get("assertion")
        entity_ref = str(raw.get("entity_ref", "")).strip()
        statement = str(raw.get("statement", "")).strip()
        # Normalize drift spellings (legalName/legal_name -> name.full) onto the
        # registry's canonical predicate BEFORE the identity key is read, so the
        # supersession chain and dedup see one stable address (docs/entity.md).
        predicate = get_registry().normalize_predicate(str(raw.get("predicate", "")).strip())
        if (
            kind not in FACT_KINDS
            or assertion not in ASSERTIONS
            or not (entity_ref and statement and predicate)
        ):
            log.warning("analysis.fact_dropped", reason="invalid fields", predicate=predicate)
            continue
        qualifier = str(raw.get("qualifier") or "").strip()
        if len(predicate) > MAX_KEY_CHARS or len(qualifier) > MAX_KEY_CHARS:
            log.warning(
                "analysis.fact_dropped",
                reason="oversized identity key",
                predicate=predicate[:80],
            )
            continue
        if len(statement) > MAX_STATEMENT_CHARS:
            log.warning(
                "analysis.fact_statement_truncated",
                predicate=predicate,
                length=len(statement),
            )
            statement = statement[:MAX_STATEMENT_CHARS]
        value_json = raw.get("value_json")
        if isinstance(value_json, dict) and len(json.dumps(value_json)) > MAX_VALUE_JSON_BYTES:
            log.warning("analysis.fact_value_json_dropped", predicate=predicate)
            value_json = None
        object_ref = raw.get("object_entity_ref")
        domain = raw.get("domain")
        facts.append(
            ExtractedFact(
                predicate=predicate,
                qualifier=qualifier,
                kind=kind,
                statement=statement,
                value_json=value_json if isinstance(value_json, dict) else None,
                assertion=assertion,
                entity_ref=entity_ref,
                object_entity_ref=str(object_ref).strip() if object_ref else None,
                temporal=_parse_temporal(raw.get("temporal")),
                # Unknown domain strings fall back to "" -> the pipeline
                # substitutes the note's domain (never trust invented codes).
                domain=domain if domain in DOMAINS else "",
                confidence=_clamp_confidence(raw.get("confidence")),
            )
        )

    # Repair backward-phrase mis-resolutions before dedup, so dedup compares
    # corrected dates (a "last night" landing on the wrong day must not look
    # like a different fact from the same phrase resolved right).
    if anchor is not None:
        repaired_facts: list[ExtractedFact] = []
        for fact in facts:
            temporal, repaired = validate_backward_temporal(fact.temporal, anchor)
            if repaired:
                log.warning(
                    "analysis.temporal_repaired",
                    scope="fact",
                    phrase=fact.temporal.phrase if fact.temporal else None,
                    predicate=fact.predicate,
                    resolved=temporal.resolved_start.isoformat()
                    if temporal and temporal.resolved_start
                    else None,
                )
                fact = replace(fact, temporal=temporal)
            repaired_facts.append(fact)
        facts = repaired_facts

    # Bind each relationship object to a mention deterministically BEFORE dedup,
    # so the dedup identity key (which includes object_entity_ref) and the
    # supersession chain see the stable, recovered edge — not the model's
    # run-to-run flip between a linked object and one folded into the statement.
    facts = link_relationship_objects(facts, mentions)

    # Dedup BEFORE the cap: restatements must not eat salience slots.
    facts = dedup_facts(facts)

    dropped_facts = max(0, len(facts) - max_facts)
    if dropped_facts:
        # The prompt's soft cap, enforced: keep the FIRST N — fact order is
        # the model's salience ranking, so the tail is the trivia the prompt
        # told it to skip (docs/ANALYSIS.md "soft cap on facts-per-note"). The
        # count rides out on the Extraction so the pipeline can flag it.
        log.warning("analysis.facts_capped", kept=max_facts, dropped=dropped_facts)
        facts = facts[:max_facts]

    tokens: list[ExtractedToken] = []
    for raw in payload.get("temporal_tokens") or []:
        if not isinstance(raw, dict):
            continue
        start = parse_datetime(raw.get("resolved_start"))
        phrase = str(raw.get("phrase") or "").strip()
        # Never store only-relative: an unresolved expression is not a token
        # (docs/ANALYSIS.md "Temporal model").
        if start is None or not phrase:
            log.warning("analysis.token_dropped", reason="unresolved", phrase=phrase)
            continue
        end = parse_datetime(raw.get("resolved_end"))
        precision = raw.get("precision")
        precision = precision if precision in PRECISIONS else "unknown"
        if anchor is not None:
            shifted, end, changed = finalize_temporal(phrase, start, end, precision, anchor)
            if changed and shifted is not None:
                start = shifted
                log.warning(
                    "analysis.temporal_repaired", scope="token", phrase=phrase,
                    resolved=start.isoformat(),
                )  # fmt: skip
            # A time-of-day token with no explicit end gains the part-of-day END
            # as a within-day RANGE, so "evening"/"last night" carries a span
            # instead of a bare instant. The START is left where it resolved so
            # it still matches the fact's valid_from (one shared token, no
            # duplicate) and supersession is untouched. Token only — facts keep
            # their valid_from/to (see finalize_temporal).
            window = _part_of_day_window(phrase)
            if window is not None and start is not None and end is None:
                window_end = _local_time_on(start, window[1], anchor)
                if window_end > start:
                    end = window_end
        kind = raw.get("kind")
        kind = "range" if end is not None else (kind if kind in TOKEN_KINDS else "point")
        rrule = raw.get("rrule")
        tokens.append(
            ExtractedToken(
                phrase=phrase,
                kind=kind,
                resolved_start=start,
                resolved_end=end,
                precision=precision,
                rrule=str(rrule) if rrule else None,
            )
        )

    return Extraction(
        title=str(payload["title"]).strip(),
        tags=tags[:MAX_TAGS],
        mentions=mentions,
        facts=facts,
        tokens=tokens,
        dropped_facts=dropped_facts,
    )


def merge_extractions(parts: list[Extraction]) -> Extraction:
    """Reduce per-group extractions (chunk-level map-reduce) into one note-level
    Extraction.

    Each group was extracted with its OWN fact budget, so a long note yields
    facts proportional to its content instead of clipping at one note-wide cap.
    The reduce reuses the very machinery that reconciles facts across NOTES:
    union the mentions and tokens, re-run the deterministic object binding over
    the FULL mention set (so a relationship whose object entity was named in a
    different group still links instead of orphaning), then dedup on the
    structural identity key so a property restated across groups collapses to
    one. dropped_facts sums each group's own truncation so the note-level
    review card still reflects a hit budget.

    A single part passes through untouched — the common short-note path stays
    byte-identical to the pre-map-reduce pipeline.
    """
    if len(parts) == 1:
        return parts[0]

    title = next((p.title for p in parts if p.title), "")

    tags: list[str] = []
    for part in parts:
        for tag in part.tags:
            if tag not in tags:
                tags.append(tag)

    mentions: list[ExtractedMention] = []
    seen_mentions: set[str] = set()
    for part in parts:
        for mention in part.mentions:
            if mention.name not in seen_mentions:
                seen_mentions.add(mention.name)
                mentions.append(mention)

    facts = [fact for part in parts for fact in part.facts]
    # Re-bind objects across the FULL mention set, then collapse cross-group
    # restatements — the same two passes parse_extraction runs per group, now
    # over the union so a cross-group edge links and a cross-group duplicate
    # dedups.
    facts = link_relationship_objects(facts, mentions)
    facts = dedup_facts(facts)

    tokens: list[ExtractedToken] = []
    seen_tokens: set[tuple[str, str]] = set()
    for part in parts:
        for token in part.tokens:
            key = (token.phrase, token.resolved_start.isoformat())
            if key not in seen_tokens:
                seen_tokens.add(key)
                tokens.append(token)

    return Extraction(
        title=title,
        tags=tags[:MAX_TAGS],
        mentions=mentions,
        facts=facts,
        tokens=tokens,
        dropped_facts=sum(part.dropped_facts for part in parts),
    )
