"""Parse the integrate.note model output into a typed IntegrationIntent.

The analog of `parse_extraction` for the integrate step: strict on the
top-level shape (a malformed payload is a permanent job failure — the adapter
already spent its re-ask), lenient on individual items (a single bad
resolution/fact is dropped and logged, never sinks the whole note).

Load-bearing: like `parse_extraction`, this is where the agent's predicate is
run through the registry normalizer (legalName/legal_name -> name.legal) BEFORE
it ever reaches the structural identity key — so I4 (key normalization) holds
for the agentic flow exactly as it does for the one-shot extractor. The agent's
self-reported confidence is preserved here but only ever *lowers* a deterministic
ceiling downstream (weight model, N11).
"""

from __future__ import annotations

from typing import Any

import structlog

from jbrain.analysis.extraction import (
    ASSERTIONS,
    FACT_KINDS,
    MAX_KEY_CHARS,
    parse_datetime,
)
from jbrain.analysis.intent import (
    SUPERSESSION_ACTIONS,
    AttestedSpan,
    EntityPairProposal,
    EntityResolution,
    IntegrationIntent,
    IntentFact,
    IntentTemporal,
    SupersessionProposal,
)
from jbrain.schema import get_registry

log = structlog.get_logger()

_RESOLUTION_MODES = frozenset({"existing", "new", "ambiguous"})

# The JSON schema the integrate.note call is constrained to. Permissive on
# optional fields; the parser enforces the rest and drops bad items.
INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["resolutions", "facts"],
    "properties": {
        "resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["mention_ref", "mode"],
                "properties": {
                    "mention_ref": {"type": "string"},
                    "mode": {"type": "string", "enum": ["existing", "new", "ambiguous"]},
                    "entity_id": {"type": ["string", "null"]},
                    "new_kind": {"type": ["string", "null"]},
                    "new_name": {"type": ["string", "null"]},
                    "cross_subject": {"type": "boolean"},
                    "chunk_id": {"type": ["string", "null"]},
                    "surface": {"type": ["string", "null"]},
                    "rationale": {"type": "string"},
                },
            },
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["entity_ref", "predicate", "kind", "assertion", "statement"],
                "properties": {
                    "entity_ref": {"type": "string"},
                    "predicate": {"type": "string"},
                    "qualifier": {"type": "string"},
                    "kind": {"type": "string"},
                    "statement": {"type": "string"},
                    "value_json": {"type": ["object", "null"]},
                    "assertion": {"type": "string"},
                    "object_entity_ref": {"type": ["string", "null"]},
                    "self_confidence": {"type": "number"},
                    "inferred": {"type": "boolean"},
                    "chunk_id": {"type": ["string", "null"]},
                    "surface": {"type": ["string", "null"]},
                    "temporal": {"type": ["object", "null"]},
                },
            },
        },
        "supersession_proposals": {"type": "array"},
        "merge_proposals": {"type": "array"},
        "distinct_proposals": {"type": "array"},
    },
}


class IntentParseError(Exception):
    """The payload does not have the documented top-level shape."""


def _span(raw: dict[str, Any]) -> AttestedSpan | None:
    chunk_id = raw.get("chunk_id")
    surface = raw.get("surface")
    if chunk_id and surface:
        return AttestedSpan(chunk_id=str(chunk_id), surface=str(surface))
    return None


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5  # a missing/garbage confidence is treated as middling


def _parse_resolution(raw: Any) -> EntityResolution | None:
    if not isinstance(raw, dict):
        return None
    ref = str(raw.get("mention_ref", "")).strip()
    mode = raw.get("mode")
    if not ref or mode not in _RESOLUTION_MODES:
        log.warning("integrate.resolution_dropped", reason="invalid mention_ref/mode")
        return None
    return EntityResolution(
        mention_ref=ref,
        mode=mode,
        proposed_entity_id=(str(raw["entity_id"]) if raw.get("entity_id") else None),
        new_kind=(str(raw["new_kind"]) if raw.get("new_kind") else None),
        new_name=(str(raw["new_name"]) if raw.get("new_name") else None),
        cross_subject=bool(raw.get("cross_subject", False)),
        attested_span=_span(raw),
        rationale=str(raw.get("rationale") or ""),
    )


def _parse_temporal(raw: Any) -> IntentTemporal | None:
    if not isinstance(raw, dict):
        return None
    start = parse_datetime(raw.get("resolved_start"))
    if start is None and not raw.get("phrase"):
        return None
    return IntentTemporal(
        phrase=(str(raw["phrase"]) if raw.get("phrase") else None),
        resolved_start=start,
        resolved_end=parse_datetime(raw.get("resolved_end")),
        precision=str(raw.get("precision") or "unknown"),
    )


def _parse_fact(raw: Any) -> IntentFact | None:
    if not isinstance(raw, dict):
        return None
    entity_ref = str(raw.get("entity_ref", "")).strip()
    statement = str(raw.get("statement", "")).strip()
    kind, assertion = raw.get("kind"), raw.get("assertion")
    # Normalize the predicate onto its canonical address BEFORE keying (I4).
    predicate = get_registry().normalize_predicate(str(raw.get("predicate", "")).strip())
    qualifier = str(raw.get("qualifier") or "").strip()
    if (
        not (entity_ref and statement and predicate)
        or kind not in FACT_KINDS
        or assertion not in ASSERTIONS
        or len(predicate) > MAX_KEY_CHARS
        or len(qualifier) > MAX_KEY_CHARS
    ):
        log.warning("integrate.fact_dropped", reason="invalid fields", predicate=predicate[:80])
        return None
    value_json = raw.get("value_json")
    return IntentFact(
        entity_ref=entity_ref,
        predicate=predicate,
        qualifier=qualifier,
        kind=kind,
        statement=statement,
        value_json=value_json if isinstance(value_json, dict) else None,
        assertion=assertion,
        object_entity_ref=(str(raw["object_entity_ref"]) if raw.get("object_entity_ref") else None),
        temporal=_parse_temporal(raw.get("temporal")),
        attested_span=_span(raw),
        self_confidence=_clamp(raw.get("self_confidence")),
        inferred=bool(raw.get("inferred", False)),
    )


def _parse_supersession(raw: Any) -> SupersessionProposal | None:
    if not isinstance(raw, dict):
        return None
    ref = str(raw.get("entity_ref", "")).strip()
    predicate = get_registry().normalize_predicate(str(raw.get("predicate", "")).strip())
    action = raw.get("action")
    if not (ref and predicate) or action not in SUPERSESSION_ACTIONS:
        log.warning("integrate.supersession_dropped", reason="invalid fields")
        return None
    return SupersessionProposal(
        entity_ref=ref,
        predicate=predicate,
        qualifier=str(raw.get("qualifier") or "").strip(),
        action=action,
        rationale=str(raw.get("rationale") or ""),
    )


def _parse_pair(raw: Any) -> EntityPairProposal | None:
    if not isinstance(raw, dict):
        return None
    a = str(raw.get("entity_a_id", "")).strip()
    b = str(raw.get("entity_b_id", "")).strip()
    if not (a and b):
        log.warning("integrate.pair_dropped", reason="missing id")
        return None
    return EntityPairProposal(
        entity_a_id=a, entity_b_id=b, rationale=str(raw.get("rationale") or "")
    )


def _collect(raw: Any, parse) -> list:
    if not isinstance(raw, list):
        return []
    return [item for item in (parse(x) for x in raw) if item is not None]


def parse_intent(
    payload: Any,
    *,
    note_id: str,
    schema_version: int,
    prompt_version: str,
    integrator_version: str,
) -> IntegrationIntent:
    """Validate the integrate.note JSON into a typed IntegrationIntent.

    Raises:
        IntentParseError: the top-level shape is wrong (permanent failure).
    """
    if not isinstance(payload, dict):
        raise IntentParseError("intent payload is not a JSON object")
    for key in ("resolutions", "facts"):
        if not isinstance(payload.get(key), list):
            raise IntentParseError(f"intent missing or mistyped field: {key!r}")
    return IntegrationIntent(
        note_id=note_id,
        schema_version=schema_version,
        prompt_version=prompt_version,
        integrator_version=integrator_version,
        entity_resolutions=_collect(payload.get("resolutions"), _parse_resolution),
        facts=_collect(payload.get("facts"), _parse_fact),
        supersession_proposals=_collect(payload.get("supersession_proposals"), _parse_supersession),
        merge_proposals=_collect(payload.get("merge_proposals"), _parse_pair),
        distinct_proposals=_collect(payload.get("distinct_proposals"), _parse_pair),
    )
