"""Measure whether the live model fills a proposed tool schema well enough to ship it.

The deterministic harness scripts a perfect model, and `evals/run.py` scores a prompt's
OUTPUT. Neither can answer the question a new tool surface raises first: will the model
actually FILL this schema? `TOOL_SURFACE.md` had to design a whole flat-scalar fallback
around not knowing, for a shape nobody had tried on this box.

So: send a candidate schema to the real model through `/api/debug/tool-probe` (which never
runs a handler) and score the call it proposes. Well-formed means every item carried all
its required non-blank string fields, and — where a schema demands a quote — that the quote
is a verbatim substring of the note. Anything less is a shape that will silently drop facts.

    JBRAIN_DEBUG_TOKEN=... uv run python -m evals.shape_probe shape 20
    JBRAIN_DEBUG_TOKEN=... uv run python -m evals.shape_probe fields 15

Two suites, asking two different questions:

- **`shape`** (W2) — is a batched array-of-objects filled at all, versus a flat
  one-fact-per-call scalar? Answered 20/20 both ways at 7.6 vs 1.0 items a turn. Ship
  batched.
- **`fields`** (the W3 six-gap re-point) — what does ADDING a field cost? R3 says a
  required field is the only reliable field, so every gap closed costs grammar on every
  element of an ≤8 batch. This suite scores the same note under the shipped six-field
  schema, a ten-field one (`kind`, `assertion`, `qualifier`, `when_end`) and an
  eleven-field one (`confidence`), then scores each ADDED field on its own terms: is the
  value in the field's vocabulary, and is it RIGHT? A field filled legally but wrongly is
  worse than no field — for `assertion` a wrong `negated` retracts a true fact, and for
  `confidence` a wrong low number holds one.

Dev-only, like the rest of `backend/evals/`, and opt-in: it costs real inference on the
box's serial GPU, roughly 20-40 s per sample.

**It measures the FIRST call and nothing after it.** With a full tool set attached the
first call is whatever the persona reaches for first, so a write tool the model only gets
to on its second move is invisible here. `/api/debug/replay` takes the same inline schemas
and does run multi-turn; use that once the surface has stubs worth feeding back.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TIMEOUT_S = 300

# One note, several unambiguous facts, a few entity kinds. Deliberately ordinary: this
# measures the SCHEMA, so anything the model could reasonably disagree about would show
# up as a shape failure and confound the reading.
NOTE = (
    "Coffee with Dana Whitfield at Ritual on Valencia this morning. She just moved to the "
    "Mission from Oakland and started at Everlane as a staff engineer in March. Her partner "
    "Theo is finishing a PhD in marine biology at Berkeley. She drives a green Subaru "
    "Outback and is allergic to shellfish."
)

SYSTEM = (
    "You are reading a note the owner just wrote, so that what it means ends up in their "
    "knowledge graph. Read it and record what it says using your tools. Clear facts you "
    "record without asking. Every fact must carry a verbatim quote from the note."
)


def _field(desc: str) -> dict[str, str]:
    return {"type": "string", "description": desc}


FACT_FIELDS = {
    "subject": _field("The entity the fact is about, as written in the note."),
    "predicate": _field("A short snake_case relation, e.g. lives_in, works_at."),
    "value": _field("The other side of the relation."),
    "quote": _field("The exact span of the note supporting this fact, copied verbatim."),
}


def _batched_tool(name: str, desc: str, key: str, fields: dict[str, Any], cap: int) -> dict:
    return {
        "name": name,
        "description": desc,
        "input_schema": {
            "type": "object",
            "properties": {
                key: {
                    "type": "array",
                    "maxItems": cap,
                    "description": "The items to record.",
                    "items": {
                        "type": "object",
                        "properties": fields,
                        "required": list(fields),
                    },
                }
            },
            "required": [key],
        },
    }


# --- suite one: the batch shape (W2, kept as the record) ---------------------

SHAPE_ARMS: dict[str, tuple[dict[str, Any], str | None, list[str] | None]] = {
    "batched_objects": (
        _batched_tool(
            "assert_fact",
            "Record facts from the note. Batch up to 8 per call.",
            "facts",
            FACT_FIELDS,
            8,
        ),
        "facts",
        list(FACT_FIELDS),
    ),
    "flat_scalar": (
        {
            "name": "assert_fact",
            "description": "Record ONE fact from the note. Call it once per fact.",
            "input_schema": {
                "type": "object",
                "properties": dict(FACT_FIELDS),
                "required": list(FACT_FIELDS),
            },
        },
        None,
        list(FACT_FIELDS),
    ),
    "resolve_objects": (
        _batched_tool(
            "resolve_entity",
            "Resolve the people, places and things the note names. Batch up to 12.",
            "entities",
            {
                "surface": _field("The name exactly as the note writes it."),
                "kind": _field("person, place, organization, thing or event."),
            },
            12,
        ),
        "entities",
        ["surface", "kind"],
    ),
    # The control that separates "arrays are hard" from "arrays OF OBJECTS are hard".
    "resolve_strings": (
        {
            "name": "resolve_entity",
            "description": "Resolve the people, places and things the note names. Batch up to 12.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "surfaces": {
                        "type": "array",
                        "maxItems": 12,
                        "description": "The names, exactly as the note writes them.",
                        "items": {"type": "string"},
                    }
                },
                "required": ["surfaces"],
            },
        },
        "surfaces",
        None,
    ),
}


# --- suite two: what each ADDED field costs ---------------------------------

# The shipped `assert_fact` fields, described as the sidecar describes them, so the
# control arm is the real surface and not a paraphrase of it.
SHIPPED = {
    "subject": _field(
        'The entity this fact is about, as the handle resolve_entity gave you ("e1"), or'
        " the exact name you resolved."
    ),
    "predicate": _field(
        "The relation, in a short lowerCamelCase or snake_case name — worksAt, livesIn,"
        " spouse, allergy, bodyWeight, medication, treatedBy, birthDate."
    ),
    "object": _field(
        'The other side of the relation. A handle ("e2") when it is another entity you'
        ' resolved; otherwise the value itself, written plainly — "412 Oak St", "178 lb".'
    ),
    "statement": _field("The fact as one plain sentence, the way it should read back."),
    "when": _field(
        "When the fact holds, as an ISO date the note actually gives: 2026, 2026-03,"
        " 2026-03-14, or a full timestamp. An empty string when the note gives no date."
    ),
    "quote": _field(
        "The words in the note this fact rests on, copied out exactly — character for"
        " character, no paraphrase."
    ),
}

# The four candidate fields that close gaps 1-4. Enumerated values live in the
# DESCRIPTION, never in a JSON-Schema `enum` (plan constraint 8: the enum × all-optional
# shape segfaults gpt-oss's harmony grammar).
#
# Each has TWO spellings, because the first pilot found the failure that decides this
# whole exercise: a required open-STRING field is filled every time (R3 holds) with a
# value the model invented rather than one from the described vocabulary — `kind:
# "weight_measurement"`, `assertion: "car_no_longer_owned"`, `confidence: "high"`.
# Required buys presence, not membership. The `_v2` spellings are the sharpened retry:
# an imperative "copy exactly one of these words", the list first and the gloss second,
# and — where the value is not a word at all — a non-string JSON type, which the grammar
# CAN enforce without an `enum`.
KIND_FIELD = _field(
    "What sort of fact this is, one word. measurement — a number read off a scale, a"
    " cuff, a meter. state — something true for a stretch of time that later changes"
    " (a job, an address, a medication regimen). event — something that happened at one"
    " moment. attribute — something timeless (a name, a birth date, an allergy)."
    " preference — what the subject likes or always chooses. relationship — a link to"
    " another entity you resolved. Use relationship whenever `object` is a handle."
)
KIND_FIELD_V2 = _field(
    "Copy exactly ONE of these six words, and never any other word:"
    " measurement, state, event, attribute, preference, relationship."
    " measurement = a number read off a scale, a cuff or a meter. state = true for a"
    " stretch of time and later changes (a job, an address, a dose). event = happened at"
    " one moment. attribute = timeless (a name, a birth date, an allergy). preference ="
    " what the subject likes or always chooses. relationship = the object is a handle"
    " for another entity. Do not invent a word; do not describe the fact here."
)
ASSERTION_FIELD = _field(
    "What the note DOES with this fact, one word. asserted — the note says it is so."
    " negated — the note says it is NOT so, or that it has ended: sold, quit, moved out,"
    ' cancelled, "no longer". questioned — the note wonders whether it is so. reported —'
    " someone else told the owner. hypothetical — it might happen, or is being"
    " considered. Use asserted unless the note plainly says otherwise."
)
ASSERTION_FIELD_V2 = _field(
    "Copy exactly ONE of these five words, and never any other word:"
    " asserted, negated, question, reported, hypothetical."
    " Write asserted for almost every fact — it means the note says this is so."
    " negated = the note says it is NOT so, or that it is over: sold, quit, cancelled,"
    ' "no longer". question = the note wonders. reported = someone else told the owner.'
    " hypothetical = it might happen. Do not invent a word; do not describe the fact"
    " here."
)
QUALIFIER_FIELD = _field(
    "The word that tells two facts apart when they share one predicate and both are"
    ' true — the audience of a nickname ("friends"), which of two diagnoses this is,'
    " which arm a reading was taken on. An empty string when nothing needs telling"
    " apart, which is most of the time."
)
QUALIFIER_FIELD_V2 = _field(
    "Almost always an empty string. Fill it ONLY when this call sends two facts with the"
    " SAME subject and the SAME predicate that are both true at once — then give each"
    ' one short word that tells them apart (the audience of a nickname, "left" or'
    ' "right" for an arm). If no other fact in this call shares this predicate, the'
    " answer is an empty string. It is not a note, a date, or a description."
)
WHEN_END_FIELD = _field(
    "When the fact STOPPED holding, in the same ISO shape as `when`, for something the"
    ' note says is over — "we lived there 2019 to 2023". An empty string when the fact'
    " still holds, or the note gives no end. Never guess one."
)
WHEN_END_FIELD_V2 = _field(
    "Almost always an empty string. Fill it ONLY when the note itself says the fact is"
    ' OVER and says when it ended — "we lived there from 2019 until 2023" ends'
    " 2023. Then write that end as an ISO date: 2023, 2023-04, 2023-04-09. If the fact"
    " is still true today, or the note gives no ending date, the answer is an empty"
    " string. Never put today's date here, and never a phrase like 'present' or 'last"
    " week'."
)
CONFIDENCE_FIELD = _field(
    "How sure you are you READ this correctly, as a number from 0 to 1. 1 when the"
    " note's words are plain. Lower only when the words themselves are hard to make out"
    " — a blurry photo, bad handwriting, an OCR line you had to guess at, a number you"
    " could not quite see. This is about legibility, not about whether the fact is true"
    " or whether the owner is right."
)
# A NUMBER, not a string carrying a number: the pilot's string spelling came back
# "high"/"low" every time, and a JSON type is the one constraint the tool grammar can
# enforce without an `enum`.
CONFIDENCE_FIELD_V2 = {
    "type": "number",
    "description": (
        "A number between 0 and 1: how sure you are you READ these words correctly."
        " Write 1 for almost every fact — the note's words are plain. Write below 0.5"
        " ONLY when the words themselves are hard to make out: a blurry photo, bad"
        " handwriting, an OCR line you had to guess at, a digit you could not quite see."
        " This is about legibility, never about whether the fact is true, whether the"
        " owner is right, or how important it is."
    ),
}

# The third confidence spelling. `_v2` measured 94/94 legal and — the number that
# matters — ZERO legible facts marked down, but on a genuinely unreadable line it landed
# ON 0.5 as often as below it, and 0.5 is not < `supersession.LOW_CONFIDENCE`. So this
# one names a concrete low number instead of a threshold to stay under.
CONFIDENCE_FIELD_V3 = {
    "type": "number",
    "description": (
        "A number between 0 and 1: how sure you are you READ these words correctly."
        " Write 1 for almost every fact — the note's words are plain. Write 0.3 or lower"
        " when you had to GUESS at the words themselves: a blurry photo, bad"
        " handwriting, an OCR line you could not make out, a digit you could not quite"
        " see. This is about legibility, never about whether the fact is true, whether"
        " the owner is right, or how important it is."
    ),
}

TEN = {
    **SHIPPED,
    "kind": KIND_FIELD,
    "assertion": ASSERTION_FIELD,
    "qualifier": QUALIFIER_FIELD,
    "when_end": WHEN_END_FIELD,
}
ELEVEN = {**TEN, "confidence": CONFIDENCE_FIELD}
TEN_V2 = {
    **SHIPPED,
    "kind": KIND_FIELD_V2,
    "assertion": ASSERTION_FIELD_V2,
    "qualifier": QUALIFIER_FIELD_V2,
    "when_end": WHEN_END_FIELD_V2,
}
ELEVEN_V2 = {**TEN_V2, "confidence": CONFIDENCE_FIELD_V2}

# The third spelling, and the one the six-gap decision actually turns on. Once the `_v2`
# arms showed that a closed WORD list is unreachable through a description at any
# sharpness, the question became which closed vocabularies a tool grammar can enforce
# WITHOUT an `enum` (plan constraint 8). There are exactly two: a JSON `number` and a
# JSON `boolean`. So the two gaps whose vocabulary a boolean can carry get a boolean.
NEGATED_FIELD = {
    "type": "boolean",
    "description": (
        "true only when the note says this fact is NOT so, or that it is OVER — sold,"
        ' quit, cancelled, moved out, "no longer", "used to". false for everything the'
        " note simply states, which is almost every fact. A wrong true erases something"
        " true, so when in doubt write false."
    ),
}
READING_FIELD = {
    "type": "boolean",
    "description": (
        "true when this fact is a NUMBER read off an instrument — a scale, a blood"
        " pressure cuff, a glucose meter, a thermometer — the kind of value that is"
        " taken again and again and keeps every past reading. false for everything"
        " else, including a dose, a price, an address and a job title."
    ),
}

# The `predicate` description that teaches the qualifier channel `assert_fact` already
# HAS: `registry.decompose_predicate` recovers a qualifier the model folded into the
# dotted path, so `name.nickname.friends` is stored as name.nickname + friends. This arm
# asks whether the model uses that spelling when the description names it.
PREDICATE_DOTTED = _field(
    "The relation, in a short lowerCamelCase or snake_case name — worksAt, livesIn,"
    " spouse, allergy, bodyWeight, medication, treatedBy, birthDate. A few relations"
    " name a SLOT rather than a single value, and take a third dotted segment saying"
    " which slot: a nickname belongs to the people who use it"
    " (name.nickname.friends, name.nickname.kids, name.nickname.work), an identifier"
    " belongs to its scheme (identifier.icd10). Write the segment for those; every"
    " other relation is two segments at most."
)

FACT_DESC = "Everything this note says, as separate facts. Send them together in ONE call, up to 8."

# The qualifier note: one subject, one relation, three values that are all true at once.
# Nothing else in it needs a qualifier, so the over-application rate is readable.
QUALIFIER_NOTE = (
    "Everyone calls Celine something different: her friends call her Sammy, her kids "
    "call her Mimi, and at work she goes by C.K. Her legal name is Celine Kitina "
    "Hopkins. She drives a Subaru and lives on Oak St."
)

# The note the field suite reads. Every added field has exactly one item in here that
# needs it and several that must NOT get it — the over-fill rate is the number that
# decides whether a field is safe, not the fill rate.
FIELD_NOTE = (
    "Weighed in at 178 lb this morning, down from 182 lb back in March. I finally sold "
    "the Civic last week so it is gone. Dana still works at Everlane. We lived at 118 "
    "Pine Ave from 2019 until 2023; Oak St is home now. Dr. Patel diagnosed me with mild "
    "asthma and separately with a vitamin D deficiency. I always take the aisle seat."
)

# The safety note (gap 6). One legible line, one explicitly unreadable one, one ordinary
# reading — so a confidence field can be scored for DISCRIMINATION rather than for being
# filled. If the model cannot separate these three it has no self-report worth wiring to
# a guard that holds facts.
OCR_NOTE = (
    "Photo of the pharmacy label from the drawer. The top line is clean: Lisinopril 10 "
    "mg, one tablet daily. The second line is smudged and half out of focus — it might "
    "be 25 mg or 2.5 mg of hydrochlorothiazide, I genuinely cannot tell which. Blood "
    "pressure this morning was 128 over 82, read straight off the cuff."
)

FIELD_SYSTEM = (
    "You are reading a note the owner just wrote, so that what it means ends up in their "
    "knowledge graph. Read it and record what it says using your tools, in ONE call. "
    "Clear facts you record without asking. Every fact must carry a verbatim quote."
)

FACT_KINDS = frozenset({"event", "measurement", "state", "attribute", "preference", "relationship"})
# `extraction.ASSERTIONS` exactly — a near-miss like "questioned" is scored ILLEGAL,
# because the parse drops a fact whose assertion is not one of these six.
ASSERTIONS = frozenset({"asserted", "negated", "hypothetical", "reported", "question", "expected"})


@dataclass
class Arm:
    """One probe arm: a schema, the note it reads, and the fields each item owes."""

    tool: dict[str, Any]
    note: str
    system: str
    items_key: str | None
    required: list[str] | None
    # Fields scored for legality/correctness beyond mere presence.
    graded: tuple[str, ...] = ()
    samples_default: int = 15


def _fact_arm(fields: dict[str, Any], note: str, graded: tuple[str, ...]) -> Arm:
    return Arm(
        tool=_batched_tool("assert_fact", FACT_DESC, "facts", fields, 8),
        note=note,
        system=FIELD_SYSTEM,
        items_key="facts",
        required=list(fields),
        graded=graded,
    )


GRADED_FOUR = ("kind", "assertion", "qualifier", "when_end")

FIELD_ARMS: dict[str, Arm] = {
    # The control: the surface as shipped, on the same note, so the added-field arms are
    # compared against this box on this note rather than against W2's different one.
    "six_shipped": _fact_arm(SHIPPED, FIELD_NOTE, ()),
    "ten_gaps_1_4": _fact_arm(TEN, FIELD_NOTE, GRADED_FOUR),
    "eleven_with_confidence": _fact_arm(ELEVEN, FIELD_NOTE, (*GRADED_FOUR, "confidence")),
    # The safety arm: the same eleven-field schema against a note whose middle fact is
    # explicitly unreadable.
    "confidence_ocr": _fact_arm(ELEVEN, OCR_NOTE, ("confidence",)),
    # The sharpened retry. One field at a time, so a failure names the field rather than
    # the batch: an eleven-field arm that degrades tells you nothing about WHICH field
    # cost the degradation.
    "v2_kind": _fact_arm({**SHIPPED, "kind": KIND_FIELD_V2}, FIELD_NOTE, ("kind",)),
    "v2_assertion": _fact_arm(
        {**SHIPPED, "assertion": ASSERTION_FIELD_V2}, FIELD_NOTE, ("assertion",)
    ),
    "v2_qualifier": _fact_arm(
        {**SHIPPED, "qualifier": QUALIFIER_FIELD_V2}, FIELD_NOTE, ("qualifier",)
    ),
    "v2_when_end": _fact_arm({**SHIPPED, "when_end": WHEN_END_FIELD_V2}, FIELD_NOTE, ("when_end",)),
    "v2_confidence": _fact_arm(
        {**SHIPPED, "confidence": CONFIDENCE_FIELD_V2}, FIELD_NOTE, ("confidence",)
    ),
    "v2_confidence_ocr": _fact_arm(
        {**SHIPPED, "confidence": CONFIDENCE_FIELD_V2}, OCR_NOTE, ("confidence",)
    ),
    # And the whole sharpened surface at once — the number that says what closing four
    # gaps together costs in well-formedness and in facts per turn.
    "v2_eleven": _fact_arm(ELEVEN_V2, FIELD_NOTE, (*GRADED_FOUR, "confidence")),
    # The typed retry: the two vocabularies a boolean can carry, and the dotted
    # qualifier the tool can already read.
    "v3_negated": _fact_arm({**SHIPPED, "negated": NEGATED_FIELD}, FIELD_NOTE, ("negated",)),
    "v3_reading": _fact_arm({**SHIPPED, "reading": READING_FIELD}, FIELD_NOTE, ("reading",)),
    "v3_dotted_qualifier": _fact_arm(
        {**SHIPPED, "predicate": PREDICATE_DOTTED}, QUALIFIER_NOTE, ("dotted",)
    ),
    # The SHIPPING schema: the six that were there plus the two fields the arms above
    # measured as fillable. Eight required fields on every element of an <=8 batch is the
    # cost of closing gaps 4 and 6, and this is the arm that says what it bought.
    "v4_shipping_eight": _fact_arm(
        {**SHIPPED, "when_end": WHEN_END_FIELD_V2, "confidence": CONFIDENCE_FIELD_V3},
        FIELD_NOTE,
        ("when_end", "confidence"),
    ),
    "v4_confidence_ocr": _fact_arm(
        {**SHIPPED, "when_end": WHEN_END_FIELD_V2, "confidence": CONFIDENCE_FIELD_V3},
        OCR_NOTE,
        ("confidence",),
    ),
}


# --- grading the added fields ------------------------------------------------
#
# Each grader answers two questions the fill rate cannot: is the value in the field's
# vocabulary, and — on the ONE item in the note that needs the field — is it right, while
# the items that do not need it are left alone. The second number is the one that decides
# a field: `assertion` filled `negated` on a fact the note asserts is a silent retraction,
# and `confidence` filled low on a legible fact is a silent hold.


def _hit(item: dict, *words: str) -> bool:
    """Whether this item is the one the note's target sentence produced. Matched on the
    quote and statement together, because the model chooses its own predicates."""
    blob = f"{item.get('quote', '')} {item.get('statement', '')} {item.get('object', '')}".lower()
    return any(w in blob for w in words)


@dataclass
class Grade:
    legal: int = 0
    illegal: int = 0
    illegal_examples: list[str] = field(default_factory=list)
    # target hits / target items seen / non-target items wrongly marked
    right: int = 0
    targets: int = 0
    over: int = 0
    over_examples: list[str] = field(default_factory=list)
    # Every value the field carried, so a summary line that reads "0 right" can be told
    # apart from one that reads "0 right because everything came back 1.0". A field is
    # decided on its DISTRIBUTION, not on a pass count.
    values: Counter[str] = field(default_factory=Counter)
    target_values: Counter[str] = field(default_factory=Counter)

    def merge(self, other: Grade) -> None:
        self.legal += other.legal
        self.illegal += other.illegal
        self.illegal_examples += other.illegal_examples
        self.right += other.right
        self.targets += other.targets
        self.over += other.over
        self.over_examples += other.over_examples
        self.values += other.values
        self.target_values += other.target_values


def _grade_kind(items: list[dict]) -> Grade:
    g = Grade()
    for item in items:
        value = str(item.get("kind", "")).strip().lower()
        g.values[value or "<blank>"] += 1
        if value in FACT_KINDS:
            g.legal += 1
        else:
            g.illegal += 1
            g.illegal_examples.append(repr(value)[:40])
        # The weight readings are the note's measurements; nothing else is.
        if _hit(item, "178 lb", "182 lb", "weighed"):
            g.targets += 1
            if value == "measurement":
                g.right += 1
        elif value == "measurement":
            g.over += 1
            g.over_examples.append(str(item.get("statement", ""))[:60])
    return g


def _grade_assertion(items: list[dict]) -> Grade:
    g = Grade()
    for item in items:
        value = str(item.get("assertion", "")).strip().lower()
        g.values[value or "<blank>"] += 1
        if value in ASSERTIONS:
            g.legal += 1
        else:
            g.illegal += 1
            g.illegal_examples.append(repr(value)[:40])
        if _hit(item, "sold the civic", "sold", "civic"):
            g.targets += 1
            if value == "negated":
                g.right += 1
        elif value not in ("asserted", ""):
            # A non-asserted modality on a fact the note plainly asserts. This is the
            # dangerous direction: `negated` is on the current floor and supersedes.
            g.over += 1
            g.over_examples.append(f"{value}: {str(item.get('statement', ''))[:52]}")
    return g


def _grade_qualifier(items: list[dict]) -> Grade:
    g = Grade()
    diagnoses: list[str] = []
    for item in items:
        value = str(item.get("qualifier", "")).strip()
        g.values[value or "<blank>"] += 1
        # Every string is a legal qualifier — storage never gates one — so legality here
        # is only "it is a string and not a sentence".
        if len(value) <= 60:
            g.legal += 1
        else:
            g.illegal += 1
            g.illegal_examples.append(value[:40])
        if _hit(item, "asthma", "vitamin d"):
            g.targets += 1
            diagnoses.append(value.lower())
        elif value:
            g.over += 1
            g.over_examples.append(f"{value}: {str(item.get('statement', ''))[:52]}")
    # The two diagnoses are what a qualifier has to tell apart: right means they got
    # DIFFERENT non-empty qualifiers (or an object that already distinguishes them).
    if len(diagnoses) >= 2 and len(set(diagnoses)) == len(diagnoses) and all(diagnoses):
        g.right = len(diagnoses)
    return g


def _iso_ok(value: str) -> bool:
    body = value.strip()
    if len(body) not in (4, 7, 10) and "T" not in body:
        return False
    return all(part.isdigit() or part == "" for part in body[:10].split("-"))


def _survives_handler(when: str, when_end: str) -> bool:
    """Whether `graphwritetools._close_interval` would actually honour this end.

    The number that decides gap 4. A raw over-application rate says the model stamps an
    end on facts that have none; what matters is how many of those REACH the graph, and
    the handler's three refusals are the filter. Reimplemented here rather than imported
    so the probe stays a plain script with no package import — and kept in step by
    `tests/unit/test_agent_graphwritetools.py`, which pins the same three shapes against
    the real function."""
    end, start = when_end.strip(), when.strip()
    if not end:
        return False
    if not start or not _iso_ok(end) or not _iso_ok(start):
        return False
    # Period end against period end: an end naming the SAME period as the start is a
    # restatement, not a close. Comparing the ISO prefixes at the coarser of the two
    # precisions is the same test one level up from datetimes.
    width = min(len(start[:10]), len(end[:10]))
    return end[:width] > start[:width]


def _grade_when_end(items: list[dict]) -> Grade:
    g = Grade()
    for item in items:
        value = str(item.get("when_end", "")).strip()
        g.values[value or "<blank>"] += 1
        if not value or _iso_ok(value):
            g.legal += 1
        else:
            g.illegal += 1
            g.illegal_examples.append(value[:40])
        survives = _survives_handler(str(item.get("when", "")), value)
        if _hit(item, "pine ave", "118 pine"):
            g.targets += 1
            g.target_values[f"{value or '<blank>'}{'' if survives else ' (refused)'}"] += 1
            if value.startswith("2023") and survives:
                g.right += 1
        elif value:
            g.over += 1
            if survives:
                g.over_examples.append(
                    f"SURVIVES {value} on when={item.get('when', '')!r}:"
                    f" {str(item.get('statement', ''))[:44]}"
                )
    return g


def _confidence(item: dict) -> float | None:
    raw = item.get("confidence")
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _grade_confidence(items: list[dict]) -> Grade:
    """The safety grade. `targets`/`right` is the SMUDGED line correctly held (< 0.5, the
    `supersession.LOW_CONFIDENCE` threshold); `over` is a legible fact wrongly held. A
    field that scores 0 on the first is useless; a field that scores anything on the
    second is worse than useless, because it holds true facts behind a review card on a
    box whose owner has no inbox to clear."""
    g = Grade()
    for item in items:
        value = _confidence(item)
        if value is None or not 0.0 <= value <= 1.0:
            g.illegal += 1
            g.illegal_examples.append(repr(item.get("confidence"))[:40])
            continue
        g.legal += 1
        g.values[f"{value:g}"] += 1
        smudged = _hit(item, "hydrochlorothiazide", "smudged", "2.5 mg", "25 mg")
        if smudged:
            g.targets += 1
            g.target_values[f"{value:g}"] += 1
            if value < 0.5:
                g.right += 1
        elif value < 0.5:
            g.over += 1
            g.over_examples.append(f"{value}: {str(item.get('statement', ''))[:52]}")
    return g


def _grade_negated(items: list[dict]) -> Grade:
    """The boolean retry on gap 2. `right` is the sold Civic marked true; `over` is any
    other fact marked true, which on the write path is a live retraction of something the
    note asserts — the failure that makes this field dangerous rather than merely
    useless."""
    g = Grade()
    for item in items:
        value = item.get("negated")
        if isinstance(value, bool):
            g.legal += 1
        else:
            g.illegal += 1
            g.illegal_examples.append(repr(value)[:40])
            value = str(value).strip().lower() == "true"
        g.values[str(bool(value)).lower()] += 1
        if _hit(item, "sold the civic", "sold", "civic"):
            g.targets += 1
            g.target_values[str(bool(value)).lower()] += 1
            if value:
                g.right += 1
        elif value:
            g.over += 1
            g.over_examples.append(str(item.get("statement", ""))[:60])
    return g


def _grade_reading(items: list[dict]) -> Grade:
    """The boolean retry on gap 1, narrowed to the one fact kind the harness's seven
    `kind` xfails actually turn on: `measurement`."""
    g = Grade()
    for item in items:
        value = item.get("reading")
        if isinstance(value, bool):
            g.legal += 1
        else:
            g.illegal += 1
            g.illegal_examples.append(repr(value)[:40])
            value = str(value).strip().lower() == "true"
        g.values[str(bool(value)).lower()] += 1
        if _hit(item, "178 lb", "182 lb", "weighed"):
            g.targets += 1
            g.target_values[str(bool(value)).lower()] += 1
            if value:
                g.right += 1
        elif value:
            g.over += 1
            g.over_examples.append(str(item.get("statement", ""))[:60])
    return g


def _grade_dotted(items: list[dict]) -> Grade:
    """Whether the model reaches for the dotted qualifier `decompose_predicate` already
    reads. `right` is a nickname predicate carrying a third segment; `over` is a dotted
    segment on a predicate that needed none — which pollutes the identity key of a fact
    nothing else would have collided with."""
    g = Grade()
    for item in items:
        predicate = str(item.get("predicate", "")).strip()
        g.legal += 1
        g.values[predicate] += 1
        nickname = _hit(item, "sammy", "mimi", "c.k.", "goes by")
        segments = predicate.count(".")
        if nickname:
            g.targets += 1
            g.target_values[predicate] += 1
            if segments >= 2:
                g.right += 1
        elif segments >= 2:
            g.over += 1
            g.over_examples.append(f"{predicate}: {str(item.get('statement', ''))[:48]}")
    return g


GRADERS = {
    "kind": _grade_kind,
    "assertion": _grade_assertion,
    "qualifier": _grade_qualifier,
    "when_end": _grade_when_end,
    "negated": _grade_negated,
    "reading": _grade_reading,
    "dotted": _grade_dotted,
    "confidence": _grade_confidence,
}


def _credentials() -> tuple[str, str]:
    """Same resolution `scripts/debug-connect.sh` uses: env first, then the gitignored
    file at the repo root. The payload is base64 JSON carrying the box url and the key."""
    payload = os.environ.get("JBRAIN_DEBUG_TOKEN", "").strip()
    if not payload:
        path = Path(__file__).resolve().parents[2] / ".jbrain-debug-token"
        if path.is_file():
            payload = path.read_text().strip()
    if not payload:
        sys.exit("no token: set JBRAIN_DEBUG_TOKEN or write ./.jbrain-debug-token")
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
        return str(claims["u"]).rstrip("/"), str(claims["k"])
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        sys.exit(f"token is not a debug payload ({{u, k}} base64 JSON): {exc}")


def _probe(url: str, key: str, tool: dict[str, Any], note: str, system: str) -> dict[str, Any]:
    body = json.dumps(
        {
            "user_text": note,
            "system": system,
            "task": "agent.turn",
            "raw_tools": [tool],
            "max_tokens": 4096,
        }
    )
    out = subprocess.run(
        [
            "curl",
            "-sS",
            "-m",
            str(TIMEOUT_S),
            "-X",
            "POST",
            "-H",
            f"Authorization: Bearer {key}",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
            f"{url}/api/debug/tool-probe",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return {"_transport": out.stderr.strip()[:200]}
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"_transport": out.stdout[:200]}


def _collect(res: dict[str, Any], items_key: str | None) -> tuple[str, list[Any], str]:
    """The proposed call's items, or a verdict naming why there are none."""
    if "_transport" in res:
        return "transport_error", [], res["_transport"]
    if res.get("error"):
        return "llm_error", [], str(res["error"])[:160]
    calls = res.get("tool_calls") or []
    if not calls:
        return "no_call", [], str(res.get("text") or "")[:120]
    items: list[Any] = []
    for call in calls:
        args = call.get("arguments")
        if not isinstance(args, dict):
            return "malformed", [], f"arguments not an object: {type(args).__name__}"
        if items_key is None:
            items.append(args)
            continue
        batch = args.get(items_key)
        if not isinstance(batch, list):
            return "malformed", [], f"{items_key} not a list: {json.dumps(args)[:100]}"
        items.extend(batch)
    if not items:
        return "empty", [], ""
    return "ok", items, ""


def _score(
    res: dict[str, Any], items_key: str | None, required: list[str] | None, note: str
) -> tuple[str, int, str, list[dict]]:
    verdict, items, detail = _collect(res, items_key)
    if verdict != "ok":
        return verdict, 0, detail, []
    for item in items:
        if required is None:
            if not isinstance(item, str) or not item.strip():
                return "malformed", len(items), f"non-string item: {item!r}", []
            continue
        if not isinstance(item, dict):
            return "malformed", len(items), f"item not an object: {item!r}", []
        # `when`, `when_end` and `qualifier` carry an explicit empty-string escape, so a
        # blank there is the schema being obeyed rather than a field going unfilled.
        blankable = {"when", "when_end", "qualifier"}
        missing = [
            k
            for k in required
            if not isinstance(item.get(k), (str, int, float))
            or (k not in blankable and not str(item.get(k)).strip())
        ]
        if missing:
            return (
                "malformed",
                len(items),
                f"missing/blank {missing} in {json.dumps(item)[:120]}",
                [],
            )
        # The quote is the whole span-attestation contract: a paraphrase cannot be located
        # in a chunk, so a plausible-looking non-verbatim quote is a silent fact loss.
        if "quote" in required and str(item["quote"]) not in note:
            return "bad_quote", len(items), f"not verbatim: {str(item['quote'])[:80]!r}", []
    typed = [i for i in items if isinstance(i, dict)]
    return "ok", len(items), "", typed


def _run(arm_name: str, arm: Arm, url: str, key: str, samples: int) -> None:
    tally: Counter[str] = Counter()
    yields: list[int] = []
    failures: list[str] = []
    grades: dict[str, Grade] = {name: Grade() for name in arm.graded}
    for i in range(samples):
        res = _probe(url, key, arm.tool, arm.note, arm.system)
        verdict, count, detail, items = _score(res, arm.items_key, arm.required, arm.note)
        tally[verdict] += 1
        if verdict == "ok":
            yields.append(count)
            for name in arm.graded:
                grades[name].merge(GRADERS[name](items))
        elif detail:
            failures.append(f"  [{i}] {verdict}: {detail}")
        print(f"{arm_name} {i + 1}/{samples}: {verdict} ({count})", flush=True)
    ok = tally["ok"]
    mean = sum(yields) / len(yields) if yields else 0.0
    print(
        f"\n=== {arm_name}: {ok}/{samples} well-formed ({100 * ok / samples:.0f}%),"
        f" mean {mean:.1f} items per turn"
    )
    for verdict, count in tally.most_common():
        if verdict != "ok":
            print(f"    {verdict}: {count}")
    for name in arm.graded:
        g = grades[name]
        total = g.legal + g.illegal
        legal = f"{g.legal}/{total}" if total else "0/0"
        right = f"{g.right}/{g.targets}" if g.targets else "n/a"
        landed = f" ({len(g.over_examples)} survive the handler)" if name == "when_end" else ""
        print(f"    {name}: legal {legal}, right-on-target {right}, over-applied {g.over}{landed}")
        if g.values:
            spread = ", ".join(f"{v}×{n}" for v, n in g.values.most_common(8))
            print(f"        values: {spread}")
        if g.target_values:
            spread = ", ".join(f"{v}×{n}" for v, n in g.target_values.most_common(6))
            print(f"        on target: {spread}")
        for example in g.illegal_examples[:3]:
            print(f"        illegal: {example}")
        for example in g.over_examples[:8]:
            print(f"        over: {example}")
    for line in failures[:6]:
        print(line)
    print(flush=True)


def main() -> None:
    argv = sys.argv[1:]
    suite = argv[0] if argv and not argv[0].isdigit() else "shape"
    rest = [a for a in argv if a.isdigit()]
    samples = int(rest[0]) if rest else 15
    only = [a for a in argv[1:] if not a.isdigit()]
    url, key = _credentials()
    if suite == "shape":
        for arm_name, (tool, items_key, required) in SHAPE_ARMS.items():
            if only and arm_name not in only:
                continue
            _run(
                arm_name,
                Arm(tool=tool, note=NOTE, system=SYSTEM, items_key=items_key, required=required),
                url,
                key,
                samples,
            )
        return
    if suite != "fields":
        sys.exit("suite is 'shape' or 'fields'")
    for arm_name, arm in FIELD_ARMS.items():
        if only and arm_name not in only:
            continue
        _run(arm_name, arm, url, key, samples)


if __name__ == "__main__":
    main()
