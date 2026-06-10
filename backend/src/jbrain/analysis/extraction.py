"""Parsing and validation of the note.extract JSON payload.

Strict on structure (a payload that isn't the documented shape is a permanent
job failure — the adapter already spent its one re-ask), lenient on individual
items: a single fact with a bogus enum is dropped and logged rather than
sinking the whole note.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo
from typing import Any

import structlog

log = structlog.get_logger()

FACT_KINDS = frozenset({"event", "measurement", "state", "attribute", "preference", "relationship"})
ASSERTIONS = frozenset({"asserted", "negated", "hypothetical", "reported", "question", "expected"})
PRECISIONS = frozenset({"instant", "day", "month", "year", "era", "unknown"})
TOKEN_KINDS = frozenset({"point", "range", "recurrence"})
DOMAINS = frozenset({"general", "health", "finance", "location"})
RESTRICTED_DOMAINS = frozenset({"health", "finance", "location"})

MAX_TAGS = 6


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


def parse_datetime(value: Any, default_tz: tzinfo = UTC) -> datetime | None:
    """ISO 8601 -> aware datetime; naive values are pinned to default_tz —
    the capture anchor's frame. The model resolves in the author's local
    frame, so an offset-less "2026-06-10" means local June 10; pinning it
    to UTC would shift day-precision dates a day early when rendered
    locally (the field off-by-one). None/unparseable -> None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=default_tz)


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
    """A fact whose validity is still in the future has not occurred yet, so
    it cannot be an asserted past `event` (docs/ANALYSIS.md "Temporal model":
    future-tense facts carry `expected`). The v2 prompt teaches this, but a
    model lapse must not land a follow-up "in 3 months" as a bare asserted
    event. A temporal-correctness rule, not a kind rule: only the assertion
    relaxes, the model's chosen kind is never rewritten.
    """
    start = fact.temporal.resolved_start if fact.temporal else None
    if start is not None and start > anchor and fact.assertion == "asserted":
        return replace(fact, assertion="expected")
    return fact


def _parse_temporal(raw: Any, default_tz: tzinfo) -> ExtractedTemporal | None:
    if not isinstance(raw, dict):
        return None
    precision = raw.get("precision")
    start = parse_datetime(raw.get("resolved_start"), default_tz)
    phrase = raw.get("phrase")
    return ExtractedTemporal(
        phrase=str(phrase) if phrase else None,
        resolved_start=start,
        resolved_end=parse_datetime(raw.get("resolved_end"), default_tz),
        precision=precision if precision in PRECISIONS else "unknown",
    )


def _clamp_confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def parse_extraction(payload: Any, *, default_tz: tzinfo = UTC) -> Extraction:
    """Validate the parsed JSON into typed extraction objects.

    default_tz is the capture anchor's frame: offset-less datetimes from the
    model are local times, not UTC.

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
        value_json = raw.get("value_json")
        object_ref = raw.get("object_entity_ref")
        domain = raw.get("domain")
        facts.append(
            ExtractedFact(
                predicate=predicate,
                qualifier=str(raw.get("qualifier") or "").strip(),
                kind=kind,
                statement=statement,
                value_json=value_json if isinstance(value_json, dict) else None,
                assertion=assertion,
                entity_ref=entity_ref,
                object_entity_ref=str(object_ref).strip() if object_ref else None,
                temporal=_parse_temporal(raw.get("temporal"), default_tz),
                # Unknown domain strings fall back to "" -> the pipeline
                # substitutes the note's domain (never trust invented codes).
                domain=domain if domain in DOMAINS else "",
                confidence=_clamp_confidence(raw.get("confidence")),
            )
        )

    tokens: list[ExtractedToken] = []
    for raw in payload.get("temporal_tokens") or []:
        if not isinstance(raw, dict):
            continue
        start = parse_datetime(raw.get("resolved_start"), default_tz)
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
                resolved_end=parse_datetime(raw.get("resolved_end"), default_tz),
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
