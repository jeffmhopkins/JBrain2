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
