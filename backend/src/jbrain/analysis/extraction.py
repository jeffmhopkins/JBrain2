"""Parsing and validation of the note.extract JSON payload.

Strict on structure (a payload that isn't the documented shape is a permanent
job failure — the adapter already spent its one re-ask), lenient on individual
items: a single fact with a bogus enum is dropped and logged rather than
sinking the whole note.
"""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import structlog

from jbrain.analysis.prompt import MAX_FACTS
from jbrain.analysis.supersession import _same_quantity

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


@dataclass(frozen=True)
class Extraction:
    title: str
    tags: list[str]
    mentions: list[ExtractedMention]
    facts: list[ExtractedFact]
    tokens: list[ExtractedToken]


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


def parse_extraction(payload: Any) -> Extraction:
    """Validate the parsed JSON into typed extraction objects.

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
        predicate = str(raw.get("predicate", "")).strip()
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

    # Dedup BEFORE the cap: restatements must not eat salience slots.
    facts = dedup_facts(facts)

    if len(facts) > MAX_FACTS:
        # The prompt's soft cap, enforced: keep the FIRST N — fact order is
        # the model's salience ranking, so the tail is the trivia the prompt
        # told it to skip (docs/ANALYSIS.md "soft cap on facts-per-note").
        log.warning("analysis.facts_capped", kept=MAX_FACTS, dropped=len(facts) - MAX_FACTS)
        facts = facts[:MAX_FACTS]

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
        kind = raw.get("kind")
        precision = raw.get("precision")
        rrule = raw.get("rrule")
        tokens.append(
            ExtractedToken(
                phrase=phrase,
                kind=kind if kind in TOKEN_KINDS else "point",
                resolved_start=start,
                resolved_end=parse_datetime(raw.get("resolved_end")),
                precision=precision if precision in PRECISIONS else "unknown",
                rrule=str(rrule) if rrule else None,
            )
        )

    return Extraction(
        title=str(payload["title"]).strip(),
        tags=tags[:MAX_TAGS],
        mentions=mentions,
        facts=facts,
        tokens=tokens,
    )
