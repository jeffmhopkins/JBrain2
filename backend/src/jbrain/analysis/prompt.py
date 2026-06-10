"""The note.extract prompt and JSON schema (docs/ANALYSIS.md "Facts").

PROMPT_VERSION is stamped on every fact and on note_analysis: it is what makes
a corpus re-run a planned, budgeted migration instead of silent drift. Bump it
whenever the system prompt or schema changes meaningfully.

v2 (field-driven): kind discipline (relocations/job changes are STATE changes,
not bare events), a canonical predicate list so re-extractions converge on one
spelling per concept, future-tense rendering rules, and an anchor stated in
the author's local frame — a UTC-frame anchor made day-precision dates land at
UTC midnight, which local rendering shows as the previous day.
"""

from datetime import datetime
from typing import Any

PROMPT_VERSION = "note-extract-v2"

# Soft cap, enforced by instruction (over-extraction is the known quality
# risk; the review-inbox rejection rate is the tuning signal).
MAX_FACTS = 12

# The convergence list the prompt pins (docs/ANALYSIS.md: schema.org-guided).
# Same concept -> same predicate across every model and prompt version is
# what keeps structural identity keys matchable.
CANONICAL_PREDICATES = (
    "homeLocation",
    "worksFor",
    "jobTitle",
    "birthDate",
    "weight",
    "height",
    "bloodPressure",
    "spouse",
    "knows",
    "owns",
    "email",
    "telephone",
    "scheduled_time",
)

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

- "predicate": canonical names, always. Use this list whenever the concept \
fits, with these exact spellings: {", ".join(CANONICAL_PREDICATES)}. \
The same concept must ALWAYS get the same predicate — where someone lives is \
homeLocation every time (never residence, moved_to, relocatedTo, address); \
who they work for is worksFor every time (never employer, works_at). For \
concepts beyond the list, use the schema.org property name when one exists; \
coin a snake_case predicate only for a genuinely novel concept with no \
schema.org fit. "qualifier" distinguishes sub-properties; use "" when none.

- "kind": one of:
  event - something occurred at a specific time;
  measurement - a numeric reading at an instant (put value and unit in \
value_json, e.g. {{"value": 182, "unit": "lb"}});
  state - a condition holding over an interval until it changes: residence \
(homeLocation), employer (worksFor), job title, relationship status, owned \
things, medication regimen;
  attribute - timeless (birthday, blood type);
  preference - a like/dislike/habit, valid from when reported;
  relationship - an edge to another entity (set object_entity_ref).

KIND DISCIPLINE — states vs events. A relocation, job change, or similar \
transition is a STATE CHANGE: you MUST emit the state fact carrying the new \
value, with valid_from = when the change happened. You may additionally emit \
the change itself as an event, but the state fact is mandatory — an \
event-only extraction loses the current value. Worked examples:
- "she just moved to Denver" -> MANDATORY state fact: predicate \
"homeLocation", kind "state", value_json {{"city": "Denver"}}, statement \
"Sarah lives in Denver.", valid_from = the move date. NOT just an event \
"relocatedTo Denver" — optionally add the move event, but never instead.
- "I started at Acme on Monday" -> MANDATORY state fact: predicate \
"worksFor", kind "state", object_entity_ref "Acme", valid_from = that \
Monday; optionally also the start event.
- "BP was 128/82 at this morning's checkup" -> "bloodPressure" is a \
measurement (a reading at an instant, never a state); the checkup itself \
may be an event fact.

- "statement": one self-contained sentence rendering the fact. Render \
resolved ABSOLUTE times in statements, never the relative phrase: "she \
wants me back in 3 months" -> "Follow-up with Dr. Patel around September \
10, 2026." (with the anchor-resolved date), not "...in 3 months".

- "assertion": asserted | negated | hypothetical | reported | question | \
expected. Second-hand claims are "reported"; "doctor wants to rule out \
diabetes" is hypothetical, NOT an asserted diabetes fact. Future or planned \
things — appointments, follow-ups, intentions — are ALWAYS "expected", \
never plain occurred events: they have not happened yet.

- "entity_ref" (and "object_entity_ref" for relationships) must exactly match \
a mention's "name" ("Me" for the author).

- "temporal": resolve every relative time phrase ("today", "last Tuesday", \
"in 3 months") against the capture anchor given with the note, IN THE \
AUTHOR'S LOCAL FRAME stated there: "today" is the anchor's local calendar \
date. Output absolute ISO 8601 carrying the anchor's UTC offset; \
day-precision values are local midnight with that offset (anchor Wednesday, \
June 10, 2026 (UTC-06:00) -> "today" = 2026-06-10T00:00:00-06:00, never UTC \
midnight of a different frame). Set "precision" honestly: instant | day | \
month | year | era | unknown. Never invent dates: if a phrase cannot be \
resolved, keep the phrase and leave resolved_start null. Use null temporal \
when the fact has no time dimension.

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

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def format_anchor(anchor: datetime) -> str:
    """The anchor as the model should read it: an explicit local datetime with
    weekday and UTC offset — "Wednesday, June 10, 2026, 5:11 PM (UTC-06:00)".

    Locale-independent on purpose (no %A/%B): the prompt contract must not
    change with the worker's locale. Naive anchors render without an offset
    rather than inventing one.
    """
    hour12 = anchor.hour % 12 or 12
    meridiem = "AM" if anchor.hour < 12 else "PM"
    base = (
        f"{_WEEKDAYS[anchor.weekday()]}, {_MONTHS[anchor.month - 1]} {anchor.day}, "
        f"{anchor.year}, {hour12}:{anchor.minute:02d} {meridiem}"
    )
    offset = anchor.utcoffset()
    if offset is None:
        return base
    total = int(offset.total_seconds() // 60)
    sign = "-" if total < 0 else "+"
    return f"{base} (UTC{sign}{abs(total) // 60:02d}:{abs(total) % 60:02d})"


def build_user_prompt(texts: list[str], *, anchor: datetime, domain: str) -> str:
    """The per-note prompt. The anchor arrives already shifted into the
    author's local frame (the pipeline rebuilds it from the stored offset):
    every relative phrase resolves against this local datetime, and the
    spelled-out "today" date forecloses UTC-frame day arithmetic."""
    content = "\n\n".join(t for t in texts if t.strip())
    return (
        f"Capture anchor (when the author wrote this note, author-local): "
        f"{format_anchor(anchor)} — ISO {anchor.isoformat()}\n"
        f'Resolve relative phrases in this local frame: "today" = '
        f"{anchor.date().isoformat()}.\n"
        f"Note capture domain: {domain}\n\n"
        f"Note content:\n{content}"
    )
