"""The note.extract prompt and JSON schema (docs/ANALYSIS.md "Facts").

PROMPT_VERSION is stamped on every fact and on note_analysis: it is what makes
a corpus re-run a planned, budgeted migration instead of silent drift. Bump it
whenever the system prompt or schema changes meaningfully.
"""

from datetime import datetime
from typing import Any

PROMPT_VERSION = "note-extract-v1"

# Soft cap, enforced by instruction (over-extraction is the known quality
# risk; the review-inbox rejection rate is the tuning signal).
MAX_FACTS = 12

SYSTEM_PROMPT = f"""You extract structured knowledge from one personal note. \
Return ONLY a JSON object matching the requested schema — no prose.

The note is a private primary source written by its author. Produce:

1. "title": a short, neutral title (max 60 characters). The title appears in \
note lists outside the note's domain: when the note's domain is "general", \
never surface health or finance details in the title or tags.

2. "tags": 3 to 6 short lowercase topical tags.

3. "mentions": every distinct person, organization, place, event, or thing \
referred to. "kind" prefers schema.org type names (Person, Organization, \
Place, Event, Product); coin a snake_case kind only when schema.org has no \
fit. Unattributed first person ("I", "me", "my") is the note's author: emit \
one mention with name "Me", kind "Person", and the pronoun as surface_text. \
Quoted or relayed first person ("Mom says: I take lisinopril") belongs to the \
speaker, not "Me". "surface_text" must be copied verbatim from the note.

4. "facts": at most {MAX_FACTS} of the most durable, useful statements. Each \
fact is one property-graph edge entity.predicate[.qualifier] -> value:
- "predicate": prefer schema.org property names where they exist (birthDate, \
worksFor, address, weight, bloodPressure); otherwise coin snake_case. \
"qualifier" distinguishes sub-properties; use "" when none.
- "kind": one of:
  event - something occurred at a specific time;
  measurement - a numeric reading at an instant (put value and unit in \
value_json, e.g. {{"value": 182, "unit": "lb"}});
  state - a condition holding over an interval (address, employer, medication \
regimen);
  attribute - timeless (birthday, blood type);
  preference - a like/dislike/habit, valid from when reported;
  relationship - an edge to another entity (set object_entity_ref).
- "statement": one self-contained sentence rendering the fact.
- "assertion": asserted | negated | hypothetical | reported | question | \
expected. Second-hand claims are "reported"; future or planned things are \
"expected"; "doctor wants to rule out diabetes" is hypothetical, NOT an \
asserted diabetes fact.
- "entity_ref" (and "object_entity_ref" for relationships) must exactly match \
a mention's "name" ("Me" for the author).
- "temporal": resolve every relative time phrase ("last Tuesday", "this \
morning") against the capture anchor given with the note, to absolute ISO \
8601 with UTC offset. Set "precision" honestly: instant | day | month | year \
| era | unknown. Never invent dates: if a phrase cannot be resolved, keep the \
phrase and leave resolved_start null. Use null temporal when the fact has no \
time dimension.
- "domain": general | health | finance | location — judged per fact, not per \
note. When unsure between general and a sensitive domain, choose the \
sensitive one.
- "confidence": 0 to 1, honest; lower it for garbled, OCR-derived, or \
inferred content.

5. "temporal_tokens": every date/time expression in the note, resolved the \
same way. "kind" is point | range | recurrence; for recurrences also emit an \
iCalendar RRULE string.

Extract less, not more: skip trivia, pleasantries, and restatements of the \
same fact."""

_TEMPORAL_PROPS: dict[str, Any] = {
    "phrase": {"type": ["string", "null"]},
    "resolved_start": {"type": ["string", "null"]},
    "resolved_end": {"type": ["string", "null"]},
    "precision": {"enum": ["instant", "day", "month", "year", "era", "unknown"]},
}

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "tags", "mentions", "facts", "temporal_tokens"],
    "properties": {
        "title": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 6},
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "kind", "surface_text"],
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string"},
                    "surface_text": {"type": "string"},
                },
            },
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "predicate",
                    "qualifier",
                    "kind",
                    "statement",
                    "value_json",
                    "assertion",
                    "entity_ref",
                    "object_entity_ref",
                    "temporal",
                    "domain",
                    "confidence",
                ],
                "properties": {
                    "predicate": {"type": "string"},
                    "qualifier": {"type": "string"},
                    "kind": {
                        "enum": [
                            "event",
                            "measurement",
                            "state",
                            "attribute",
                            "preference",
                            "relationship",
                        ]
                    },
                    "statement": {"type": "string"},
                    "value_json": {"type": ["object", "null"]},
                    "assertion": {
                        "enum": [
                            "asserted",
                            "negated",
                            "hypothetical",
                            "reported",
                            "question",
                            "expected",
                        ]
                    },
                    "entity_ref": {"type": "string"},
                    "object_entity_ref": {"type": ["string", "null"]},
                    "temporal": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "required": ["phrase", "resolved_start", "resolved_end", "precision"],
                        "properties": _TEMPORAL_PROPS,
                    },
                    "domain": {"enum": ["general", "health", "finance", "location"]},
                    "confidence": {"type": "number"},
                },
            },
        },
        "temporal_tokens": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "phrase",
                    "kind",
                    "resolved_start",
                    "resolved_end",
                    "precision",
                    "rrule",
                ],
                "properties": {
                    **_TEMPORAL_PROPS,
                    "phrase": {"type": "string"},
                    "kind": {"enum": ["point", "range", "recurrence"]},
                    "rrule": {"type": ["string", "null"]},
                },
            },
        },
    },
}


def build_user_prompt(texts: list[str], *, anchor: datetime, domain: str) -> str:
    """The per-note prompt: capture anchor (with timezone — the resolution
    target for every relative phrase), capture domain, and the chunk texts."""
    content = "\n\n".join(t for t in texts if t.strip())
    return (
        f"Capture anchor (note creation time): {anchor.isoformat()}\n"
        f"Note capture domain: {domain}\n\n"
        f"Note content:\n{content}"
    )
