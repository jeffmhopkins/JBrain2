"""The note.extract prompt and JSON schema (docs/ANALYSIS.md "Facts").

PROMPT_VERSION is stamped on every fact and on note_analysis: it is what makes
a corpus re-run a planned, budgeted migration instead of silent drift. Bump it
whenever the system prompt or schema changes meaningfully.
"""

from datetime import datetime
from typing import Any

PROMPT_VERSION = "note-extract-v2"

# Facts-per-note cap: taught by instruction here, enforced server-side in
# extraction.parse_extraction (over-extraction is the known quality risk;
# the review-inbox rejection rate is the tuning signal).
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
- "predicate": prefer schema.org property names where they exist; otherwise \
coin snake_case. "qualifier" distinguishes sub-properties; use "" when none. \
Re-use the SAME predicate name for the SAME concept across notes — the \
predicate is the identity key that lets a later note update an earlier fact, \
so drifting between synonyms (relocatedTo vs moved_to) silently forks the \
history. Prefer these canonical schema.org property names:
  * residence / where someone lives, a move or relocation: homeLocation
  * employer / where someone works: worksFor
  * mailing or street address: address
  * spouse / marriage: spouse
  * birthday / date of birth: birthDate
  * job title / role: jobTitle
  * weight: weight; height: height; blood pressure: bloodPressure
  * a scheduled appointment's time: scheduledTime

- "kind": choose by what the fact IS, not by the verb tense of the sentence:
  state - a durable condition that HOLDS OVER AN INTERVAL and changes by being \
replaced: where someone lives (homeLocation), their employer (worksFor), \
their address, marital status, a current medication regimen. A residence, \
employer, address, or marital change is a STATE change: emit the durable fact \
as a `state` on the canonical predicate above (homeLocation/worksFor/...). \
The new state supersedes the old one on the same predicate, giving the \
property a current-value-plus-history rail. You MAY ALSO emit the move itself \
as a separate `event` (kind=event, e.g. predicate "moved"), but the durable, \
supersedable fact is the `state` and it is required.
  event - something that OCCURRED at a specific time and is then immutable \
("saw Dr. Patel June 3"); never use `event` for a condition that persists.
  measurement - a numeric reading at an instant (put value and unit in \
value_json, e.g. {{"value": 182, "unit": "lb"}}); accumulates as a series.
  attribute - timeless and singular (birthday, blood type).
  preference - a like/dislike/habit, valid from when reported.
  relationship - an edge to another entity (set object_entity_ref).
- "statement": one self-contained sentence rendering the fact.
- "assertion": asserted | negated | hypothetical | reported | question | \
expected. Second-hand claims are "reported". A FUTURE or planned thing that \
has not happened yet is "expected", NEVER an asserted `event`: "she wants me \
back in 3 months", "dentist appointment next Friday", and "I'll start the new \
job in July" are all `expected`. "doctor wants to rule out diabetes" is \
hypothetical, NOT an asserted diabetes fact.
- "entity_ref" (and "object_entity_ref" for relationships) must exactly match \
a mention's "name" ("Me" for the author).
- "temporal": resolve every relative time phrase ("last Tuesday", "this \
morning", "in 3 months") against the capture anchor given with the note, to \
absolute ISO 8601 with UTC offset, preserving the anchor's local date. Set \
"precision" honestly: instant | day | month | year | era | unknown. Never \
invent dates: if a phrase cannot be resolved, keep the phrase and leave \
resolved_start null. Use null temporal when the fact has no time dimension.
- "domain": general | health | finance | location — judged per fact, not per \
note. When unsure between general and a sensitive domain, choose the \
sensitive one.
- "confidence": 0 to 1, honest; lower it for garbled, OCR-derived, or \
inferred content.

5. "temporal_tokens": every date/time expression in the note, resolved the \
same way. "kind" is point | range | recurrence; for recurrences also emit an \
iCalendar RRULE string.

Worked example — "Sarah moved to Denver." (a relocation is a state change):
  {{"predicate": "homeLocation", "qualifier": "", "kind": "state", \
"statement": "Sarah lives in Denver.", "value_json": {{"place": "Denver"}}, \
"assertion": "asserted", "entity_ref": "Sarah", "object_entity_ref": null, \
"temporal": null, "domain": "location", "confidence": 0.9}}
A later note "Sarah actually moved to Boulder" emits the same homeLocation \
`state` predicate with "Boulder" — matching predicates let Boulder supersede \
Denver instead of forking two unrelated facts.

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
