# Red-Team Dossier: Subject-as-Object Extraction Failures

**Investigation role:** Agent C — prompt-failure-mode analysis of the known
"Jeff is married to Celine Hopkins" extraction lapse.  
**Model under test:** `xai:grok-4.3` via `note.extract` task  
**Prompt version:** `note-extract-v4`  
**Date:** 2026-06-11

---

## 1. Failure-Mode Analysis

### 1.1 What the prompt literally requires

The mentions instruction (`prompt.py` line 29) reads:

> `"mentions": every distinct person, organization, place, event, or thing referred to.`

"Referred to" is the operative phrase. "Celine Hopkins" is textually referred
to in "Jeff is married to Celine Hopkins." A literal reading therefore demands
her as a Person mention. Yet the lapse shows she was not emitted. Why?

**Gap 1 — no positive obligation binds the object person to a mention.**  
The prompt never states that *every person who appears in a relationship fact's
`object_entity_ref` MUST also appear as a mention*. `extraction.py` line
369 shows `object_entity_ref` is simply stored as a string without any
cross-validation against the `mentions` array — a dangling ref mints a
provisional entity downstream but does not cause a parse failure and does not
cause a loud warning during the extraction parse. From the model's vantage
point there is zero schema-level signal that "Celine" must be a mention; the
field is just a nullable string.

**Gap 2 — worked examples are owner-centric and possess-or-move only.**  
Both worked examples in the prompt (lines 114–132) have the author ("Me") or a
named secondary (`Summer`) as the grammatical *subject*, and the object is
either a place (`Denver`) or an animal (`Bella`, `Ricky`). In every example
the object entity that has `object_entity_ref` set is NOT a `Person`; it is a
`Place`, a `dog`, or a `rat`. There is no worked example where the object of a
relationship is another `Person`. The model learns from few-shot exemplars: the
entire illustrated relationship subgraph for `Person`-typed entities lives on
the subject side.

**Gap 3 — the canonical predicate list (lines 52–58) lists `spouse` without
illustrating its object.**  
Line 52–53 reads: `* spouse / marriage: spouse`. The hint is one-directional:
it teaches the predicate name but provides no example of the full edge
`Me.spouse → {object_entity_ref: "Celine"}`. Combined with Gap 2, the model
knows to emit the `spouse` fact on the subject but has no reinforcement that
the object is itself a Person requiring a mention.

**Gap 4 — "Extract less, not more" (line 132) creates asymmetric pressure.**  
The final sentence of the system prompt is:

> `Extract less, not more: skip trivia, pleasantries, and restatements of the same fact.`

Under token/salience competition, the model must decide whether "Celine
Hopkins" is its own extractable entity or merely a value inside a fact's
`value_json`. The pressure to compress pushes toward folding her into
`value_json: {"spouse": "Celine Hopkins"}` — which satisfies the letter of the
`spouse` predicate hint — rather than minting a separate mention and setting
`object_entity_ref`. The fact becomes structurally inert as an edge.

**Gap 5 — third-person notes remove the "Me" anchor, eliminating the most
reinforced extraction path.**  
Every scenario in the existing test harness that exercises relationship facts
has "Me" as `entity_ref`. The prompt's "Unattributed first person" rule (lines
33–35) gives the model a strong, rehearsed anchor for the subject: emit a "Me"
mention. When the note is third-person ("Jeff is married to Celine"), NEITHER
party maps to the practiced "Me" path. Both are unfamiliar territory, and the
model has learned from examples that the subject is the interesting party —
Jeff gets emitted, Celine does not.

**Gap 6 — mutual/inverse edges are never discussed.**  
For a `spouse` predicate, a complete graph requires both `Jeff.spouse →
Celine` and `Celine.spouse → Jeff` (or at minimum that both are mentions so
linking can derive the inverse). The prompt has no language requiring the
model to emit inverse edges for symmetric predicates. For asymmetric predicates
(`parent`, `worksFor`), the inverse is different in kind — a child fact, an
`employee` fact — also not mentioned. The compounding effect: a dropped object
mention PLUS absent inverse means two edges are lost instead of one.

### 1.2 Why the model specifically drops the object person

Beyond the prompt gaps, there are salience and cognitive-bias reasons:

- **Subject primacy bias:** language models are trained overwhelmingly on text
  where the grammatical subject is the topic-entity and the object is a
  modifier. The model's attention weights the subject entity more heavily during
  extraction.
- **Fame / relevance asymmetry:** when one party is more contextually prominent
  (the note author, a more frequently mentioned person), the model treats the
  other as peripheral. In "Jeff is married to Celine Hopkins" inside a corpus
  of Jeff's notes, "Celine" may feel like a modifier to Jeff's status, not an
  independent entity.
- **Singleton-mention pruning:** under the 12-fact cap (line 16) and the
  `extract less, not more` pressure, a mention with no standalone facts
  attached feels like overhead. The model prunes it.
- **Predicate-as-value conflation:** `spouse` sounds like a property with a
  value (the spouse's name as a string), not like a directed graph edge. The
  model is more likely to represent it as `value_json: {"name": "Celine
  Hopkins"}` than as a `relationship` kind with a valid `object_entity_ref`.

---

## 2. Failure-Mode Table

| Failure mode | Trigger | What gets dropped |
|---|---|---|
| **Object-person not minted as mention** | The grammatical object of a relationship verb is a Person, but the prompt has no requirement linking `object_entity_ref` to a mention | The object person's `mention` entry; `object_entity_ref` is null or the fact is typed wrong (`state` with name in `value_json` instead of `relationship`) |
| **Third-person subject drop** | Neither party in the note is the author; neither maps to the rehearsed "Me" anchor | Subject person is emitted, object person is not — or neither is emitted because the model looks for a "Me" anchor first |
| **Pronoun-object erasure** | Object is a pronoun ("her", "him") with no antecedent in the same note | Object person is unknown → model emits null `object_entity_ref` or skips the relationship fact entirely |
| **Bare-first-name only object** | Object is identified by first name only with no earlier context; model deems it insufficiently identified | Object person omitted from mentions under "never normalize a reference mention into an invented proper name" misread |
| **Inverse-edge absence** | Symmetric predicates (`spouse`, `sibling`) and asymmetric predicates (`parent`, `worksFor`) require inverse edges; prompt has no instruction for them | One direction of the edge is present; the complementary edge (Celine.spouse → Jeff; child.parent → Jeff) never emitted |
| **Salience competition / cap starvation** | Note has many facts; the 12-fact cap is reached before mutual/inverse edges are emitted | Inverse edges are the tail items cut by the `facts[:MAX_FACTS]` slice in `extraction.py` line 385 |
| **value_json folding** | "Extract less, not more" pressure causes model to represent the object person as a string inside `value_json` rather than as a `relationship` kind | `kind` is set to `state` or `attribute` instead of `relationship`; `object_entity_ref` is null; object person has no mention |
| **Chained entity drop** | Multiple people connected by relationships in one sentence ("Jeff's sister married Celine's brother") | Two or more of the four parties are not minted as mentions; edges between non-subject pairs are silently absent |
| **Distractor salience theft** | A more concrete or dramatic fact in the same note (a medical reading, a job change) dominates salience under the cap | The relationship fact is emitted but the object person mention is dropped because the model prioritises the "important" entity |
| **Mutual-status blindness** | "is married to" implies a symmetric state but the model treats it as a one-way attribute of the subject | Only one direction is emitted; the object's symmetric state is not captured |

---

## 3. Ten Adversarial Cases

---

### Case 1 — Third-person, neither party is author

**Note:**
```
Jeff is married to Celine Hopkins.
```

**Attack:** Third-person subject drop + object-person-not-minted + mutual-status blindness

**Likely current extraction (the lapse):**
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

**Expected extraction:**
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
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Celine Hopkins's spouse is Jeff.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Celine Hopkins",
      "object_entity_ref": "Jeff",
      "temporal": null,
      "domain": "general",
      "confidence": 0.95
    }
  ]
}
```

**Why it matters:** This is the exact known lapse. Celine Hopkins disappears
from the knowledge graph entirely. No future notes mentioning her can link to
an existing entity; the spouse relation becomes a dead-end string; the inverse
edge (Celine's marital status) is never established.

---

### Case 2 — Pronoun object with antecedent in same sentence

**Note:**
```
Jeff married her last spring.
```

**Attack:** Pronoun-object erasure (antecedent absent from note)

**Likely current extraction:**
```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "event",
      "statement": "Jeff married someone last spring.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": null,
      "temporal": {"phrase": "last spring", "resolved_start": "2026-03-20T00:00:00+00:00", "resolved_end": "2026-06-20T00:00:00+00:00", "precision": "month"},
      "domain": "general",
      "confidence": 0.6
    }
  ]
}
```

**Expected extraction:**
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
      "statement": "Jeff married an unnamed person last spring.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "her",
      "temporal": {"phrase": "last spring", "resolved_start": "2026-03-20T00:00:00+00:00", "resolved_end": "2026-06-20T00:00:00+00:00", "precision": "month"},
      "domain": "general",
      "confidence": 0.75
    }
  ]
}
```

**Why it matters:** The pronoun "her" still denotes a real person in the
owner's life. Even without a name, a mention preserves the resolver's ability
to later link the pronoun to a known entity via the relationship hop layer
(ANALYSIS.md "Alias resolution"). Suppressing it makes the marriage event
unlinked and unresolvable. The prompt says "never normalize a reference mention
into an invented proper name" — the correct reading is to emit the pronoun
literally as surface_text, not to omit the mention altogether.

---

### Case 3 — Chained multi-entity: four people in one sentence

**Note:**
```
Jeff's sister married Celine's brother last weekend.
```

**Attack:** Chained entity drop — up to four Person mentions required; subject
is a relational alias ("Jeff's sister"), object is a relational alias
("Celine's brother"); neither is "Me."

**Likely current extraction:**
```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Celine", "kind": "Person", "surface_text": "Celine"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "state",
      "statement": "Jeff's sister married Celine's brother.",
      "value_json": {"note": "sister of Jeff married brother of Celine"},
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": null,
      "temporal": {"phrase": "last weekend", "resolved_start": "2026-06-06T00:00:00+00:00", "resolved_end": "2026-06-07T00:00:00+00:00", "precision": "day"},
      "domain": "general",
      "confidence": 0.7
    }
  ]
}
```

**Expected extraction:**
```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Jeff's sister", "kind": "Person", "surface_text": "Jeff's sister"},
    {"name": "Celine", "kind": "Person", "surface_text": "Celine"},
    {"name": "Celine's brother", "kind": "Person", "surface_text": "Celine's brother"}
  ],
  "facts": [
    {
      "predicate": "sibling",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff's sister is a sibling of Jeff.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Jeff's sister",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "sibling",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Celine's brother is a sibling of Celine.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Celine",
      "object_entity_ref": "Celine's brother",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff's sister married Celine's brother last weekend.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff's sister",
      "object_entity_ref": "Celine's brother",
      "temporal": {"phrase": "last weekend", "resolved_start": "2026-06-06T00:00:00+00:00", "resolved_end": "2026-06-07T00:00:00+00:00", "precision": "day"},
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Celine's brother married Jeff's sister last weekend.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Celine's brother",
      "object_entity_ref": "Jeff's sister",
      "temporal": {"phrase": "last weekend", "resolved_start": "2026-06-06T00:00:00+00:00", "resolved_end": "2026-06-07T00:00:00+00:00", "precision": "day"},
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

**Why it matters:** The four-entity chain is the most compounded form of the
failure. Two relational aliases ("Jeff's sister," "Celine's brother") act as
both the subject and object of the marriage edge and as objects of sibling
edges anchored on the known parties. The model is likely to collapse the entire
sentence into a single blurry fact on "Jeff" or emit zero relationship-typed
facts. Each dropped mention is a person permanently absent from the graph;
each dropped inverse is a broken navigation path.

---

### Case 4 — Object buried mid-sentence after a subordinate clause

**Note:**
```
Since moving to Austin, Jeff has been living with his wife, Monica Chen, and their two kids.
```

**Attack:** Object buried mid-sentence; relational alias ("his wife") precedes
the proper name; distractor facts ("moving to Austin," "two kids") compete for
salience.

**Likely current extraction:**
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
      "statement": "Jeff lives in Austin.",
      "value_json": {"place": "Austin"},
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "location",
      "confidence": 0.9
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "state",
      "statement": "Jeff's wife is Monica Chen.",
      "value_json": {"name": "Monica Chen"},
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "general",
      "confidence": 0.8
    }
  ]
}
```

**Expected extraction:**
```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Monica Chen", "kind": "Person", "surface_text": "Monica Chen"},
    {"name": "Austin", "kind": "Place", "surface_text": "Austin"}
  ],
  "facts": [
    {
      "predicate": "homeLocation",
      "qualifier": "",
      "kind": "state",
      "statement": "Jeff lives in Austin.",
      "value_json": {"place": "Austin"},
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "location",
      "confidence": 0.9
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff's spouse is Monica Chen.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Monica Chen",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Monica Chen's spouse is Jeff.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Monica Chen",
      "object_entity_ref": "Jeff",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

**Why it matters:** The relational alias ("his wife") is more salient than the
proper name that follows it. The model anchors the spouse fact on the alias
semantics, folds Monica Chen into `value_json`, and never emits her as an
independent mention. The homeLocation fact also competes for the slot, pushing
the model toward cap-safe compression. Monica Chen vanishes from the entity
graph even though she is the most precisely named person in the note.

---

### Case 5 — Bare first name as object, full name available only for subject

**Note:**
```
Sarah Okonkwo is the parent of Lucas.
```

**Attack:** Bare-first-name-only object + inverse edge (parent/child) absence
+ asymmetric relation that requires two different predicate names for its two
directions.

**Likely current extraction:**
```json
{
  "mentions": [
    {"name": "Sarah Okonkwo", "kind": "Person", "surface_text": "Sarah Okonkwo"}
  ],
  "facts": [
    {
      "predicate": "parent",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Sarah Okonkwo is the parent of Lucas.",
      "value_json": {"child": "Lucas"},
      "assertion": "asserted",
      "entity_ref": "Sarah Okonkwo",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "general",
      "confidence": 0.85
    }
  ]
}
```

**Expected extraction:**
```json
{
  "mentions": [
    {"name": "Sarah Okonkwo", "kind": "Person", "surface_text": "Sarah Okonkwo"},
    {"name": "Lucas", "kind": "Person", "surface_text": "Lucas"}
  ],
  "facts": [
    {
      "predicate": "parent",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Sarah Okonkwo is the parent of Lucas.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Sarah Okonkwo",
      "object_entity_ref": "Lucas",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "children",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Lucas is a child of Sarah Okonkwo.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Lucas",
      "object_entity_ref": "Sarah Okonkwo",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

**Why it matters:** "Lucas" is a bare first name — ANALYSIS.md ("Bare first
names") says bare first names auto-link only if a single matching entity
exists. If Lucas is never minted as a mention, he has no entity to auto-link
against. The prompt's instruction "never normalize a reference mention into an
invented proper name" is misread by the model as justification for suppressing
bare-first-name objects entirely. The asymmetric inverse (`children` vs
`parent`) is a second distinct edge the prompt never teaches, compounding the
loss.

---

### Case 6 — Distractor fact steals the cap slot that would carry the object mention

**Note:**
```
Had a checkup today — BP was 128/82, weight 191 lb, fasting glucose 98 mg/dL. 
Also, my colleague Dave Reyes is engaged to Priya Sharma.
```

**Attack:** Distractor-salience theft + cap starvation (health measurements
consume early cap slots; the relationship with both Person objects is the tail
entry most likely to be cut or compressed).

**Likely current extraction:**
```json
{
  "mentions": [
    {"name": "Me", "kind": "Person", "surface_text": "Had"},
    {"name": "Dave Reyes", "kind": "Person", "surface_text": "Dave Reyes"}
  ],
  "facts": [
    {
      "predicate": "bloodPressure",
      "qualifier": "",
      "kind": "measurement",
      "statement": "Blood pressure was 128/82 today.",
      "value_json": {"systolic": 128, "diastolic": 82, "unit": "mmHg"},
      "assertion": "asserted",
      "entity_ref": "Me",
      "object_entity_ref": null,
      "temporal": {"phrase": "today", "resolved_start": "2026-06-11T00:00:00+00:00", "resolved_end": null, "precision": "day"},
      "domain": "health",
      "confidence": 0.95
    },
    {
      "predicate": "weight",
      "qualifier": "",
      "kind": "measurement",
      "statement": "Weight was 191 lb today.",
      "value_json": {"value": 191, "unit": "lb"},
      "assertion": "asserted",
      "entity_ref": "Me",
      "object_entity_ref": null,
      "temporal": {"phrase": "today", "resolved_start": "2026-06-11T00:00:00+00:00", "resolved_end": null, "precision": "day"},
      "domain": "health",
      "confidence": 0.95
    },
    {
      "predicate": "glucose",
      "qualifier": "fasting",
      "kind": "measurement",
      "statement": "Fasting glucose was 98 mg/dL today.",
      "value_json": {"value": 98, "unit": "mg/dL"},
      "assertion": "asserted",
      "entity_ref": "Me",
      "object_entity_ref": null,
      "temporal": {"phrase": "today", "resolved_start": "2026-06-11T00:00:00+00:00", "resolved_end": null, "precision": "day"},
      "domain": "health",
      "confidence": 0.95
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "state",
      "statement": "Dave Reyes is engaged to Priya Sharma.",
      "value_json": {"fiancee": "Priya Sharma"},
      "assertion": "asserted",
      "entity_ref": "Dave Reyes",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "general",
      "confidence": 0.8
    }
  ]
}
```

**Expected extraction:**
```json
{
  "mentions": [
    {"name": "Me", "kind": "Person", "surface_text": "Had"},
    {"name": "Dave Reyes", "kind": "Person", "surface_text": "Dave Reyes"},
    {"name": "Priya Sharma", "kind": "Person", "surface_text": "Priya Sharma"}
  ],
  "facts": [
    {
      "predicate": "bloodPressure",
      "qualifier": "",
      "kind": "measurement",
      "statement": "Blood pressure was 128/82 today.",
      "value_json": {"systolic": 128, "diastolic": 82, "unit": "mmHg"},
      "assertion": "asserted",
      "entity_ref": "Me",
      "object_entity_ref": null,
      "temporal": {"phrase": "today", "resolved_start": "2026-06-11T00:00:00+00:00", "resolved_end": null, "precision": "day"},
      "domain": "health",
      "confidence": 0.95
    },
    {
      "predicate": "weight",
      "qualifier": "",
      "kind": "measurement",
      "statement": "Weight was 191 lb today.",
      "value_json": {"value": 191, "unit": "lb"},
      "assertion": "asserted",
      "entity_ref": "Me",
      "object_entity_ref": null,
      "temporal": {"phrase": "today", "resolved_start": "2026-06-11T00:00:00+00:00", "resolved_end": null, "precision": "day"},
      "domain": "health",
      "confidence": 0.95
    },
    {
      "predicate": "glucose",
      "qualifier": "fasting",
      "kind": "measurement",
      "statement": "Fasting glucose was 98 mg/dL today.",
      "value_json": {"value": 98, "unit": "mg/dL"},
      "assertion": "asserted",
      "entity_ref": "Me",
      "object_entity_ref": null,
      "temporal": {"phrase": "today", "resolved_start": "2026-06-11T00:00:00+00:00", "resolved_end": null, "precision": "day"},
      "domain": "health",
      "confidence": 0.95
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Dave Reyes is engaged to Priya Sharma.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Dave Reyes",
      "object_entity_ref": "Priya Sharma",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Priya Sharma is engaged to Dave Reyes.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Priya Sharma",
      "object_entity_ref": "Dave Reyes",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

**Why it matters:** This case exercises the `facts[:MAX_FACTS]` truncation
in `extraction.py` line 385. The model's salience ordering puts health
measurements first (concrete numbers, high confidence) and the relationship
fact last. If the model also attempts to emit a checkup event fact, it hits the
12-fact cap before emitting Priya's inverse edge. Priya Sharma may appear in
the mentions array but with no facts — or she may not appear at all because the
model decided her mention was redundant without a linked fact. The cap
enforcement in the pipeline then silently discards the inverse even if the
model tried to emit it.

---

### Case 7 — Subject and object reversed: "more famous" party is the object

**Note:**
```
Taylor's manager is Scooter Klein.
```

**Attack:** Salience-fame asymmetry — the model may treat "Taylor" as the
prominent entity (subject) and represent "Scooter Klein" as a mere modifier
value. The `worksFor` vs `manages` inversion is also a correctness trap:
the correct subject for `worksFor` is Scooter (he works for Taylor or her
label), but for `manages` the subject is Scooter and the object is Taylor.

**Likely current extraction:**
```json
{
  "mentions": [
    {"name": "Taylor", "kind": "Person", "surface_text": "Taylor"}
  ],
  "facts": [
    {
      "predicate": "worksFor",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Taylor's manager is Scooter Klein.",
      "value_json": {"manager": "Scooter Klein"},
      "assertion": "asserted",
      "entity_ref": "Taylor",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "general",
      "confidence": 0.8
    }
  ]
}
```

**Expected extraction:**
```json
{
  "mentions": [
    {"name": "Taylor", "kind": "Person", "surface_text": "Taylor"},
    {"name": "Scooter Klein", "kind": "Person", "surface_text": "Scooter Klein"}
  ],
  "facts": [
    {
      "predicate": "manages",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Scooter Klein manages Taylor.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Scooter Klein",
      "object_entity_ref": "Taylor",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "worksFor",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Scooter Klein works for Taylor (as manager).",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Scooter Klein",
      "object_entity_ref": "Taylor",
      "temporal": null,
      "domain": "general",
      "confidence": 0.85
    }
  ]
}
```

**Why it matters:** This case exercises the fame/salience asymmetry and the
subject-primacy bias simultaneously. The grammatical subject is Taylor, so the
model anchors the fact on Taylor's entity and folds the manager into
`value_json`. But the semantically correct edge is Scooter-manages-Taylor: the
entity doing the managing is the agent. Both Scooter's entity AND his
directional edges are lost, making the employment relationship non-navigable
from either entity.

---

### Case 8 — Employer fact with both an object-person drop and a compounding inverse

**Note:**
```
Marcus hired Elena Vasquez as lead engineer last month.
```

**Attack:** Asymmetric employer/employee relation + inverse-edge absence +
object-person not minted. The `worksFor` canonical predicate (prompt line 54)
is `Elena.worksFor → Marcus's company / Marcus`, but the note is framed around
Marcus as agent. The inverse (Marcus's `employee` or `hires` edge toward Elena)
is not in the canonical predicate list at all.

**Likely current extraction:**
```json
{
  "mentions": [
    {"name": "Marcus", "kind": "Person", "surface_text": "Marcus"}
  ],
  "facts": [
    {
      "predicate": "hires",
      "qualifier": "",
      "kind": "event",
      "statement": "Marcus hired Elena Vasquez as lead engineer last month.",
      "value_json": {"employee": "Elena Vasquez", "role": "lead engineer"},
      "assertion": "asserted",
      "entity_ref": "Marcus",
      "object_entity_ref": null,
      "temporal": {"phrase": "last month", "resolved_start": "2026-05-01T00:00:00+00:00", "resolved_end": "2026-05-31T00:00:00+00:00", "precision": "month"},
      "domain": "general",
      "confidence": 0.85
    }
  ]
}
```

**Expected extraction:**
```json
{
  "mentions": [
    {"name": "Marcus", "kind": "Person", "surface_text": "Marcus"},
    {"name": "Elena Vasquez", "kind": "Person", "surface_text": "Elena Vasquez"}
  ],
  "facts": [
    {
      "predicate": "worksFor",
      "qualifier": "",
      "kind": "state",
      "statement": "Elena Vasquez works for Marcus.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Elena Vasquez",
      "object_entity_ref": "Marcus",
      "temporal": {"phrase": "last month", "resolved_start": "2026-05-01T00:00:00+00:00", "resolved_end": null, "precision": "month"},
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "jobTitle",
      "qualifier": "",
      "kind": "attribute",
      "statement": "Elena Vasquez's job title is lead engineer.",
      "value_json": {"title": "lead engineer"},
      "assertion": "asserted",
      "entity_ref": "Elena Vasquez",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "hires",
      "qualifier": "",
      "kind": "event",
      "statement": "Marcus hired Elena Vasquez as lead engineer last month.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Marcus",
      "object_entity_ref": "Elena Vasquez",
      "temporal": {"phrase": "last month", "resolved_start": "2026-05-01T00:00:00+00:00", "resolved_end": null, "precision": "month"},
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

**Why it matters:** The prompt's canonical predicate list (line 54) teaches
`worksFor` as the durable state predicate for employment, but the note frames
the sentence around Marcus (the hirer) as subject. The model emits one event
fact on Marcus and folds Elena into `value_json`, never emitting the
`worksFor` state fact on Elena's entity — the very predicate the prompt
nominates as canonical. This is a triple failure: Elena as mention, the state
fact with correct `entity_ref` = Elena, and the `object_entity_ref` = Marcus
on that state fact.

---

### Case 9 — Negated relation: object person exists only in the negation

**Note:**
```
Jeff is not related to David Stern.
```

**Attack:** Object-person-not-minted under a negated assertion. The model may
reason that a negated relationship requires no entity node for the excluded
party; the prompt's `"negated"` assertion value (lines 90, 92) is illustrated
only for future-tense planning, not for negated relations.

**Likely current extraction:**
```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"}
  ],
  "facts": [
    {
      "predicate": "relatedTo",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff is not related to David Stern.",
      "value_json": {"note": "David Stern"},
      "assertion": "negated",
      "entity_ref": "Jeff",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

**Expected extraction:**
```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "David Stern", "kind": "Person", "surface_text": "David Stern"}
  ],
  "facts": [
    {
      "predicate": "relatedTo",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff is not related to David Stern.",
      "value_json": null,
      "assertion": "negated",
      "entity_ref": "Jeff",
      "object_entity_ref": "David Stern",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

**Why it matters:** The `distinct_from` permanent edge (ANALYSIS.md "Negative
knowledge") is seeded by negated relationship facts. If `object_entity_ref` is
null, the pipeline cannot construct the `distinct_from` constraint — the entity
David Stern is never created, and any future note mentioning him has no
existing entity to reject a merge against. The negation becomes informationally
inert. The model is especially likely to drop the object here because
"David Stern is not Jeff's relative" feels like information about Jeff, not
information about David Stern.

---

### Case 10 — Symmetric sibling relation across a clause boundary, one party named only by role

**Note:**
```
Priya's older brother runs the family business; he married into the Reyes family when he wed Carmen Reyes three years ago.
```

**Attack:** Pronoun-object erasure ("he") chained to a full-name object
("Carmen Reyes") after a clause boundary; Priya's brother appears only as a
relational alias and a pronoun, never named; four implicit entities (Priya,
Priya's brother, Carmen Reyes, the Reyes family); the sibling inverse and the
spouse inverse both need emitting; temporal ("three years ago") is relative and
must be resolved.

**Likely current extraction:**
```json
{
  "mentions": [
    {"name": "Priya", "kind": "Person", "surface_text": "Priya"},
    {"name": "Carmen Reyes", "kind": "Person", "surface_text": "Carmen Reyes"}
  ],
  "facts": [
    {
      "predicate": "manages",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Priya's older brother runs the family business.",
      "value_json": {"role": "family business owner"},
      "assertion": "asserted",
      "entity_ref": "Priya",
      "object_entity_ref": null,
      "temporal": null,
      "domain": "general",
      "confidence": 0.7
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "state",
      "statement": "Carmen Reyes married into the family three years ago.",
      "value_json": {"family": "Reyes"},
      "assertion": "asserted",
      "entity_ref": "Carmen Reyes",
      "object_entity_ref": null,
      "temporal": {"phrase": "three years ago", "resolved_start": "2023-06-11T00:00:00+00:00", "resolved_end": null, "precision": "year"},
      "domain": "general",
      "confidence": 0.75
    }
  ]
}
```

**Expected extraction:**
```json
{
  "mentions": [
    {"name": "Priya", "kind": "Person", "surface_text": "Priya"},
    {"name": "Priya's brother", "kind": "Person", "surface_text": "Priya's older brother"},
    {"name": "Carmen Reyes", "kind": "Person", "surface_text": "Carmen Reyes"}
  ],
  "facts": [
    {
      "predicate": "sibling",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Priya's brother is a sibling of Priya.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Priya",
      "object_entity_ref": "Priya's brother",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "sibling",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Priya is a sibling of Priya's brother.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Priya's brother",
      "object_entity_ref": "Priya",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Priya's brother married Carmen Reyes three years ago.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Priya's brother",
      "object_entity_ref": "Carmen Reyes",
      "temporal": {"phrase": "three years ago", "resolved_start": "2023-06-11T00:00:00+00:00", "resolved_end": null, "precision": "year"},
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Carmen Reyes married Priya's brother three years ago.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Carmen Reyes",
      "object_entity_ref": "Priya's brother",
      "temporal": {"phrase": "three years ago", "resolved_start": "2023-06-11T00:00:00+00:00", "resolved_end": null, "precision": "year"},
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

**Why it matters:** This is the maximum-difficulty compound case. "He" crosses
a semicolon clause boundary and must resolve to "Priya's older brother" — a
relational alias that itself has no proper name. The model must emit "Priya's
brother" as a mention using the surface text "Priya's older brother" (verbatim,
per the prompt's `surface_text` rule), set him as subject of the spouse fact,
set Carmen Reyes as object, and emit four edges total (two sibling, two spouse).
Every failure mode except "cap starvation" fires simultaneously: pronoun
erasure, relational-alias subject, named-person object, clause-boundary
crossing, symmetric inverse absence, and the compound "he" coreference that the
prompt explicitly disallows guessing about ("the pipeline's resolver owns
identity"). The expected extraction is the correct annotated ideal; the likely
extraction shows how badly the graph degrades with two facts instead of four
and three missing mention-entity nodes.

---

## Summary of Failure Modes Found

1. **Object-person not minted as mention** — the most fundamental gap: the
   prompt's mention instruction uses "referred to" but provides no worked
   example where the object of a relationship fact is a Person; there is zero
   schema enforcement requiring `object_entity_ref` to match a mention.

2. **Third-person subject drop** — neither party maps to the "Me" anchor that
   the prompt's examples and all existing test scenarios rehearse; the model
   defaults to emitting only the grammatical subject.

3. **value_json folding under compression pressure** — "Extract less, not more"
   (prompt.py line 132) trains the model to compress object persons into
   `value_json` string fields instead of minting them as mentions with
   `relationship`-typed facts and valid `object_entity_ref`.

4. **Inverse-edge silence** — no prompt instruction covers symmetric predicates
   (spouse, sibling) or asymmetric inverses (parent/child, hires/worksFor);
   every relationship in the worked examples is one-directional.

5. **Clause-boundary and pronoun chaining** — when the object person is
   introduced by pronoun ("he," "her") that must be resolved across a clause
   boundary to a relational alias, both the alias entity and the proper-name
   entity downstream are dropped, compounding into the maximum-loss scenario.

**Single most damning case:** Case 10 (Priya's brother / Carmen Reyes). It
fires all five failure modes simultaneously — pronoun-object erasure, relational
alias as subject, clause-boundary coreference, symmetric spouse inverse absence,
AND sibling inverse absence — collapsing four required edges and three required
mentions into two blurry facts with null object refs and one of the four people
(Priya's brother as an independent entity) entirely absent from the knowledge
graph, unresolvable by the alias layer because no `entity_ref` pointing to him
was ever written.
