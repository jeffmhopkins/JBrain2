# Grammatical Taxonomy of the Object-Person Extraction Lapse

## Framing

The known lapse — a note reading "Jeff is married to Celine Hopkins" producing
only one Person mention and a bare `spouse` fact with no `object_entity_ref` —
is not an isolated hallucination. It is a *systematic* failure rooted in how
the grammatical object of a relational predicate is handled by the extraction
prompt. When the entity at the **grammatical object position** is a person, the
extractor must (a) emit a mention for that person and (b) set `object_entity_ref`
to that mention's `name`. The current prompt succeeds at this for first-person
notes because the author-subject ("Me") is already a privileged anchor: the
instructions spend a full sentence on "Unattributed first person … is the
note's author." No comparable anchor exists for the **object-position person in
a third-person sentence**. The model therefore defaults to treating the object
noun phrase as a string value in `statement` rather than a typed entity that
deserves its own mention row.

The prompt's only worked example of a `relationship` fact (`me.employer → Acme`)
has the object be an Organization, not a Person. The animal-decomposition example
("My dog's name is Bella") concerns ownership, where the owned entity is clearly
a distinct non-human thing. Neither example demonstrates the pattern where both
ends of a relationship are `Person` entities inferred from a single sentence.
The instruction at prompt.py line 75 (`- "kind": … relationship - an edge to
another entity (set object_entity_ref)`) is correct but under-specified: it
does not say that the object entity must *also* appear as a mention, nor that
the object noun phrase in a Person-to-Person sentence must be scanned for
personhood. The phrase "every distinct person … referred to" (line 28) covers
it in principle but is overridden in practice by the model's tendency to settle
for a string rendering once a `statement` has been written.

A second structural gap reinforces the lapse: the system has **no reciprocal or
inverse-edge logic** (confirmed in ANALYSIS.md "Entities" section and in the
fact-grammar table). This means that even when both Person mentions are emitted
correctly, the extractor emits only `Jeff.spouse → Celine` and never
`Celine.spouse → Jeff`. The pipeline has no mechanism to mint the mirror edge.
This is not a prompt failure — it is an intentional architectural gap — but it
compounds the object-person lapse: if Celine is never mentioned, she never
becomes a provisional entity at all, and the marriage is represented as a
one-party state rather than a bilateral relationship. The taxonomy below is
therefore organized around two related but distinct failure modes: **F1 —
object person not emitted as a mention**, and **F2 — mutual/reciprocal edge
absent** (the second is noted but marked out-of-scope for a prompt fix).

---

## Taxonomy Table

| # | Construction | Example | Why the object person is likely dropped |
|---|---|---|---|
| 1 | Copular state ("is married to") | "Jeff is married to Celine Hopkins." | Object follows a preposition; the copula + preposition chain makes Celine look like a value phrase, not an agent. The model renders the whole prepositional phrase as `statement` text. |
| 2 | Transitive active ("married", "hired") | "Jeff married Celine Hopkins last spring." | Classic SVO; Jeff is agent, Celine is patient. Patient-position persons are under-represented in the training signal for the `relationship` kind; the model emits the event on Jeff only. |
| 3 | Passive voice ("was hired by") | "Celine Hopkins was hired by Jeff's firm." | Surface subject is the patient; the agent is demoted to a by-phrase. The model may emit a `worksFor` fact on Celine (subject) but miss the employer entity entirely, or mint an Organization mention for the firm while losing Jeff as an indirect agent. |
| 4 | Reciprocal / conjoined subject ("X and Y married") | "Jeff and Celine Hopkins got married in 2018." | No syntactic object exists; both parties are in the subject NP. The model typically anchors the fact on the first-named person only. |
| 5 | Appositive ("Jeff's wife, Celine Hopkins, …") | "Jeff's wife, Celine Hopkins, starts her new job Monday." | Celine is introduced parenthetically; the main clause is about her job. The possessive-kinship phrase tags her as a relation of Jeff but the model treats the appositive as context, not a separate entity requiring a bilateral fact. |
| 6 | Prepositional-object cohabitation ("lives with") | "Jeff has been living with Celine Hopkins for two years." | The prepositional object is a co-participant, not a destination or topic. The model may emit a `homeLocation` state on Jeff and omit Celine entirely. |
| 7 | Ditransitive ("introduced Y to Z") | "Jeff introduced Celine Hopkins to his boss." | Three participants; the model tends to anchor on Jeff (agent) and the indirect object (boss) and drop Celine (direct object) from the mention list. |
| 8 | Relative clause ("whom Jeff married") | "Celine Hopkins, whom Jeff married in 2019, lives in Austin." | The relative clause embeds the relationship; the main clause is about Celine's location. The model typically emits a `homeLocation` state on Celine and a bare `homeLocation` mention, ignoring the embedded marriage relation entirely. |
| 9 | Possessive-of-person ("Jeff's business partner Celine") | "Jeff's business partner Celine Hopkins joined the board." | The possessive NP encodes a relationship; the predicate is the head noun ("business partner"). The model may emit a `worksFor` fact on Celine (board) but not the `colleague` or `knows` relationship to Jeff. |
| 10 | Pronoun object ("married her") | "Jeff married her last spring." | Object is a pronoun; there is no surface form to use as `name` or `surface_text` for a mention. The model has no candidate string for the entity and silently omits both the mention and the `object_entity_ref`. |

---

## Ten Test Cases

Each case uses the exact mention shape `{name, kind, surface_text}` and the
exact fact shape `{predicate, qualifier, kind, statement, value_json,
assertion, entity_ref, object_entity_ref, temporal, domain, confidence}` from
`EXTRACTION_SCHEMA`. The anchor date used for temporal resolution is
`2026-06-11T10:00:00-06:00` throughout for consistency.

---

### Case 1 — Copular state: "X is married to Y"

```
note: "Jeff is married to Celine Hopkins."
construction: copular-state ("is [predicate] to")
```

**likely_current_extraction** (the lapse):

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "state",
      "statement": "Jeff is married to Celine Hopkins.",
      "value_json": {"spouse": "Celine Hopkins"},
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

`object_entity_ref` is null; Celine is folded into `value_json` as a bare
string rather than a typed entity mention. No Celine mention is emitted.

**expected_extraction**:

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff's spouse is Celine Hopkins.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Celine Hopkins",
      "temporal": null,
      "domain": "general",
      "confidence": 0.95
    }
  ]
}
```

Mutual/reciprocal note: the pipeline should also represent `Celine
Hopkins.spouse → Jeff`, but no inverse-edge mechanism exists today; this is
an architectural gap, not a prompt gap.

**why_it_matters**: Celine never becomes a provisional entity, so no future
note about Celine can be linked to her, and the marriage is invisible from
her side of the graph.

---

### Case 2 — Transitive active: "X married Y"

```
note: "Jeff married Celine Hopkins last spring."
construction: transitive-active (SVO event)
```

**likely_current_extraction** (the lapse):

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "state",
      "statement": "Jeff married Celine Hopkins last spring.",
      "value_json": {"spouse": "Celine Hopkins"},
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": null,
      "temporal": {
        "phrase": "last spring",
        "resolved_start": "2026-03-20T00:00:00-06:00",
        "resolved_end": "2026-06-20T00:00:00-06:00",
        "precision": "month"
      },
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

**expected_extraction**:

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff's spouse is Celine Hopkins.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Celine Hopkins",
      "temporal": {
        "phrase": "last spring",
        "resolved_start": "2026-03-20T00:00:00-06:00",
        "resolved_end": "2026-06-20T00:00:00-06:00",
        "precision": "month"
      },
      "domain": "general",
      "confidence": 0.95
    }
  ]
}
```

Mutual/reciprocal note: `Celine Hopkins.spouse → Jeff` is the missing mirror
edge; unrepresentable without inverse-edge logic.

**why_it_matters**: The marriage event anchors useful temporal data (last
spring); if Celine is not an entity, her side of the temporal record is
permanently missing.

---

### Case 3 — Passive voice: "Y was hired by X"

```
note: "Celine Hopkins was hired by Jeff's firm, Apex Partners."
construction: passive-voice (patient as grammatical subject, agent in by-phrase)
```

**likely_current_extraction** (the lapse):

```json
{
  "mentions": [
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"},
    {"name": "Apex Partners", "kind": "Organization", "surface_text": "Apex Partners"}
  ],
  "facts": [
    {
      "predicate": "worksFor",
      "qualifier": "",
      "kind": "state",
      "statement": "Celine Hopkins works for Apex Partners.",
      "value_json": {"organization": "Apex Partners"},
      "assertion": "asserted",
      "entity_ref": "Celine Hopkins",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "general",
      "confidence": 0.88
    }
  ]
}
```

Jeff is not mentioned at all; `object_entity_ref` on the `worksFor` fact is
null even though "Apex Partners" is a mention (the model serializes it into
`value_json` rather than setting the ref). The possessive relationship
"Jeff's firm" — encoding that Jeff owns or leads Apex Partners — is not
extracted.

**expected_extraction**:

```json
{
  "mentions": [
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"},
    {"name": "Apex Partners", "kind": "Organization", "surface_text": "Apex Partners"},
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff's"}
  ],
  "facts": [
    {
      "predicate": "worksFor",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Celine Hopkins works for Apex Partners.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Celine Hopkins",
      "object_entity_ref": "Apex Partners",
      "temporal": null,
      "domain": "general",
      "confidence": 0.92
    },
    {
      "predicate": "owns",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff is associated with Apex Partners as owner or principal.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Apex Partners",
      "temporal": null,
      "domain": "general",
      "confidence": 0.75
    }
  ]
}
```

Mutual/reciprocal note: "hired by" implies Apex Partners should have a
`employee` or `member` edge back to Celine; not representable without inverse
logic.

**why_it_matters**: Passive voice inverts the surface subject/object; the
agent buried in the by-phrase is systematically missed even when it names a
known person.

---

### Case 4 — Reciprocal / conjoined subject: "X and Y married"

```
note: "Jeff and Celine Hopkins got married in 2018."
construction: reciprocal-verb with conjoined subject (no grammatical object)
```

**likely_current_extraction** (the lapse):

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "state",
      "statement": "Jeff got married in 2018.",
      "value_json": {"spouse": "Celine Hopkins"},
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": null,
      "temporal": {
        "phrase": "in 2018",
        "resolved_start": "2018-01-01T00:00:00-06:00",
        "resolved_end": "2018-12-31T23:59:59-06:00",
        "precision": "year"
      },
      "domain": "general",
      "confidence": 0.87
    }
  ]
}
```

Celine is in `value_json` as a string; no Celine mention; only Jeff anchors
the fact. The conjoined subject structure means neither party occupies the
canonical object position, so the model anchors on the first-named subject.

**expected_extraction**:

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff's spouse is Celine Hopkins.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Celine Hopkins",
      "temporal": {
        "phrase": "in 2018",
        "resolved_start": "2018-01-01T00:00:00-06:00",
        "resolved_end": "2018-12-31T23:59:59-06:00",
        "precision": "year"
      },
      "domain": "general",
      "confidence": 0.95
    }
  ]
}
```

Mutual/reciprocal note: "Jeff and Celine married" is inherently symmetric;
`Celine Hopkins.spouse → Jeff` is the mirror edge. This construction most
clearly illustrates why no single fact direction is semantically complete.

**why_it_matters**: Conjoined-subject reciprocal verbs are the most common
form in personal notes ("we got married", "they moved in together") and have
no syntactic object position for the extractor to anchor on.

---

### Case 5 — Appositive: "Jeff's wife, Celine Hopkins, …"

```
note: "Jeff's wife, Celine Hopkins, starts her new job at Meridian Health on Monday."
construction: appositive (relational possessive NP introducing the entity)
```

**likely_current_extraction** (the lapse):

```json
{
  "mentions": [
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"},
    {"name": "Meridian Health", "kind": "Organization", "surface_text": "Meridian Health"}
  ],
  "facts": [
    {
      "predicate": "worksFor",
      "qualifier": "",
      "kind": "state",
      "statement": "Celine Hopkins starts working at Meridian Health on Monday.",
      "value_json": {"organization": "Meridian Health"},
      "assertion": "asserted",
      "entity_ref": "Celine Hopkins",
      "object_entity_ref": null,
      "temporal": {
        "phrase": "on Monday",
        "resolved_start": "2026-06-15T00:00:00-06:00",
        "resolved_end": null,
        "precision": "day"
      },
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

Jeff is not emitted as a mention. The appositive "Jeff's wife" — which
encodes a `spouse` relationship — is silently discarded as introductory
context. `object_entity_ref` on `worksFor` is null.

**expected_extraction**:

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff's"},
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"},
    {"name": "Meridian Health", "kind": "Organization", "surface_text": "Meridian Health"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff's spouse is Celine Hopkins.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Celine Hopkins",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "worksFor",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Celine Hopkins works for Meridian Health.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Celine Hopkins",
      "object_entity_ref": "Meridian Health",
      "temporal": {
        "phrase": "on Monday",
        "resolved_start": "2026-06-15T00:00:00-06:00",
        "resolved_end": null,
        "precision": "day"
      },
      "domain": "general",
      "confidence": 0.92
    }
  ]
}
```

Mutual/reciprocal note: the spouse edge here is inferred from a possessive
NP, not a main-clause predicate; the mirror `Celine Hopkins.spouse → Jeff`
is still absent.

**why_it_matters**: Appositives are the most common way a note introduces a
person through their relationship to another person; the relationship is
treated as a grammatical framing device rather than a extractable fact.

---

### Case 6 — Prepositional-object cohabitation: "X lives with Y"

```
note: "Jeff has been living with Celine Hopkins for two years."
construction: prepositional-object co-participant ("lives with")
```

**likely_current_extraction** (the lapse):

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"}
  ],
  "facts": [
    {
      "predicate": "homeLocation",
      "qualifier": "",
      "kind": "state",
      "statement": "Jeff lives with Celine Hopkins.",
      "value_json": {"cohabitant": "Celine Hopkins"},
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "location",
      "confidence": 0.8
    }
  ]
}
```

The model maps "lives with" to `homeLocation` (plausible schema.org
grounding) but treats Celine as a string value in `value_json`. No Celine
mention; no `relationship` fact representing the cohabitation edge.

**expected_extraction**:

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"}
  ],
  "facts": [
    {
      "predicate": "livesWithPerson",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff lives with Celine Hopkins.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Celine Hopkins",
      "temporal": {
        "phrase": "for two years",
        "resolved_start": "2024-06-11T00:00:00-06:00",
        "resolved_end": null,
        "precision": "year"
      },
      "domain": "location",
      "confidence": 0.88
    }
  ]
}
```

Mutual/reciprocal note: cohabitation is symmetric; `Celine Hopkins.livesWithPerson
→ Jeff` is the mirror edge.

**why_it_matters**: "Lives with" is a location-domain relationship where
schema.org has no perfect fit; the model defaults to `homeLocation` (a place
predicate) and drops the person-typed object entirely.

---

### Case 7 — Ditransitive: "X introduced Y to Z"

```
note: "Jeff introduced Celine Hopkins to his business partner, Marcus Webb."
construction: ditransitive (agent, direct object, indirect object)
```

**likely_current_extraction** (the lapse):

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Marcus Webb", "kind": "Person", "surface_text": "Marcus Webb"}
  ],
  "facts": [
    {
      "predicate": "knows",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff introduced someone to his business partner Marcus Webb.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Marcus Webb",
      "temporal": null,
      "domain": "general",
      "confidence": 0.75
    }
  ]
}
```

Celine (the direct object — the introduced party) is dropped entirely. The
model preserves the indirect object (Marcus, the recipient of the
introduction) because he is explicitly titled "business partner" and carries
a full name. The central event participant is lost.

**expected_extraction**:

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"},
    {"name": "Marcus Webb", "kind": "Person", "surface_text": "Marcus Webb"}
  ],
  "facts": [
    {
      "predicate": "introduced",
      "qualifier": "",
      "kind": "event",
      "statement": "Jeff introduced Celine Hopkins to Marcus Webb.",
      "value_json": {"to": "Marcus Webb"},
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Celine Hopkins",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "colleague",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Marcus Webb is Jeff's business partner.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Marcus Webb",
      "temporal": null,
      "domain": "general",
      "confidence": 0.88
    }
  ]
}
```

Mutual/reciprocal note: introductions create a `knows` edge between Celine
and Marcus; that emergent relationship is also absent from any single-fact
rendering.

**why_it_matters**: In ditransitive frames the direct object (the person most
affected by the event) is paradoxically the most likely to be dropped when
the indirect object carries a prominent relational title.

---

### Case 8 — Relative clause: "Celine, whom Jeff married, …"

```
note: "Celine Hopkins, whom Jeff married in 2019, recently moved to Austin."
construction: non-restrictive relative clause embedding the relationship
```

**likely_current_extraction** (the lapse):

```json
{
  "mentions": [
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"},
    {"name": "Austin", "kind": "Place", "surface_text": "Austin"}
  ],
  "facts": [
    {
      "predicate": "homeLocation",
      "qualifier": "",
      "kind": "state",
      "statement": "Celine Hopkins recently moved to Austin.",
      "value_json": {"place": "Austin"},
      "assertion": "asserted",
      "entity_ref": "Celine Hopkins",
      "object_entity_ref": null,
      "temporal": {
        "phrase": "recently",
        "resolved_start": null,
        "resolved_end": "2026-06-11T00:00:00-06:00",
        "precision": "unknown"
      },
      "domain": "location",
      "confidence": 0.9
    }
  ]
}
```

The relative clause "whom Jeff married in 2019" is ignored entirely. Jeff is
not emitted as a mention. The embedded marriage event and its temporal anchor
(2019) are lost.

**expected_extraction**:

```json
{
  "mentions": [
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"},
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Austin", "kind": "Place", "surface_text": "Austin"}
  ],
  "facts": [
    {
      "predicate": "homeLocation",
      "qualifier": "",
      "kind": "state",
      "statement": "Celine Hopkins lives in Austin.",
      "value_json": {"place": "Austin"},
      "assertion": "asserted",
      "entity_ref": "Celine Hopkins",
      "object_entity_ref": null,
      "temporal": {
        "phrase": "recently",
        "resolved_start": null,
        "resolved_end": "2026-06-11T00:00:00-06:00",
        "precision": "unknown"
      },
      "domain": "location",
      "confidence": 0.9
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff's spouse is Celine Hopkins.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Celine Hopkins",
      "temporal": {
        "phrase": "in 2019",
        "resolved_start": "2019-01-01T00:00:00-06:00",
        "resolved_end": null,
        "precision": "year"
      },
      "domain": "general",
      "confidence": 0.92
    }
  ]
}
```

Mutual/reciprocal note: the marriage temporal anchor (2019) should also seed
`Celine Hopkins.spouse → Jeff`; the precision of that anchor is lost when the
clause is skipped.

**why_it_matters**: Relative clauses that "parenthesize" a relationship are a
natural narrative device; the model treats them as stylistic subordination
and extracts only the main-clause proposition.

---

### Case 9 — Possessive-of-person: "Jeff's business partner Celine"

```
note: "Jeff's business partner Celine Hopkins joined the Apex Partners board last quarter."
construction: possessive-of-person (relational noun phrase as pre-modifier)
```

**likely_current_extraction** (the lapse):

```json
{
  "mentions": [
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"},
    {"name": "Apex Partners", "kind": "Organization", "surface_text": "Apex Partners"}
  ],
  "facts": [
    {
      "predicate": "memberOf",
      "qualifier": "",
      "kind": "state",
      "statement": "Celine Hopkins joined the Apex Partners board last quarter.",
      "value_json": {"organization": "Apex Partners"},
      "assertion": "asserted",
      "entity_ref": "Celine Hopkins",
      "object_entity_ref": null,
      "temporal": {
        "phrase": "last quarter",
        "resolved_start": "2026-01-01T00:00:00-06:00",
        "resolved_end": "2026-03-31T23:59:59-06:00",
        "precision": "month"
      },
      "domain": "general",
      "confidence": 0.88
    }
  ]
}
```

Jeff is not emitted as a mention. The possessive-NP relational predicate
("business partner") is discarded as a descriptive modifier. `object_entity_ref`
on `memberOf` is null.

**expected_extraction**:

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff's"},
    {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"},
    {"name": "Apex Partners", "kind": "Organization", "surface_text": "Apex Partners"}
  ],
  "facts": [
    {
      "predicate": "colleague",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Celine Hopkins is Jeff's business partner.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Celine Hopkins",
      "temporal": null,
      "domain": "general",
      "confidence": 0.88
    },
    {
      "predicate": "memberOf",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Celine Hopkins is a member of the Apex Partners board.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Celine Hopkins",
      "object_entity_ref": "Apex Partners",
      "temporal": {
        "phrase": "last quarter",
        "resolved_start": "2026-01-01T00:00:00-06:00",
        "resolved_end": "2026-03-31T23:59:59-06:00",
        "precision": "month"
      },
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

Mutual/reciprocal note: `colleague` is inherently symmetric; `Celine
Hopkins.colleague → Jeff` is the missing mirror.

**why_it_matters**: Possessive-of-person is how English encodes most kinship
and professional relationships attributively; the relational noun is the
predicate but it appears as a modifier, not a verb, so the model treats it as
description rather than a fact to extract.

---

### Case 10 — Pronoun object: "Jeff married her"

```
note: "Jeff married her last spring. They've been together since college."
construction: pronoun-object (the object person has no surface name in the note)
```

**likely_current_extraction** (the lapse):

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "state",
      "statement": "Jeff married someone last spring.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": null,
      "temporal": {
        "phrase": "last spring",
        "resolved_start": "2026-03-20T00:00:00-06:00",
        "resolved_end": "2026-06-20T00:00:00-06:00",
        "precision": "month"
      },
      "domain": "general",
      "confidence": 0.7
    }
  ]
}
```

No pronoun mention is emitted (the prompt does not authorize pronoun mentions
for third-person pronouns — only "Me" for first-person). `object_entity_ref`
is null because there is no candidate mention name to reference.

**expected_extraction**:

The prompt's existing rules for third-person pronouns do not authorize
inventing a proper name ("a reference mention of 'the rat' keeps that
reference phrase as its name"). The correct behaviour is to emit a pronoun
mention using the pronoun surface text as both `name` and `surface_text`,
flagging it as an unresolved reference for the entity linker:

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "her", "kind": "Person", "surface_text": "her"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff's spouse is a person referred to as 'her'.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "her",
      "temporal": {
        "phrase": "last spring",
        "resolved_start": "2026-03-20T00:00:00-06:00",
        "resolved_end": "2026-06-20T00:00:00-06:00",
        "precision": "month"
      },
      "domain": "general",
      "confidence": 0.75
    }
  ]
}
```

Mutual/reciprocal note: even with the pronoun mention correctly emitted,
`her.spouse → Jeff` cannot be built without inverse-edge logic; and "her"
as an entity name is unresolvable without additional context.

**why_it_matters**: This is the hardest construction — the object person has
no surface name, so the lapse is architecturally forced unless the prompt
explicitly authorizes pronoun-as-reference-mention for third-person pronouns
by analogy with the existing "the rat" reference-mention rule.

---

## Five-Line Summary

1. **Copular-state** ("is married to"): the sharpest and most common case —
   the prepositional-object person is folded into `value_json` as a string
   instead of being emitted as a mention with `object_entity_ref` set.
2. **Passive voice** inverts surface roles; the by-phrase agent is missed even
   when named.
3. **Conjoined-subject reciprocal** ("Jeff and Celine married") has no
   syntactic object position at all, forcing the model to pick one participant
   as the anchor and drop the other.
4. **Relative clause and appositive** embed the relationship as subordinate
   syntax; the main-clause fact is extracted and the embedded relationship
   discarded.
5. **Pronoun object** is the irreducible hard case: no surface name exists, so
   no mention can be emitted without a policy change extending the
   reference-mention rule to third-person pronouns.

**Sharpest single example: Case 1** — "Jeff is married to Celine Hopkins."
This is the original lapse note verbatim. It is a main-clause, fully-explicit,
no-ambiguity sentence with both parties named; the only reason Celine is
dropped is that "Celine Hopkins" occupies the prepositional-object position
rather than the syntactic-object position, and the prompt's `relationship`
instruction does not specify that the object noun phrase of a relational
predicate must be scanned for personhood and emitted as a typed mention.
