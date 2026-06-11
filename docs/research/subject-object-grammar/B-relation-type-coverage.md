# B — Relation-Type / Semantic Coverage of the Subject–Object Grammar Lapse

## Framing

### Functional vs Symmetric/Inverse: two orthogonal distinctions the system conflates

The pipeline's `supersession.py` defines `FUNCTIONAL_PREDICATES` (line 24):

```python
FUNCTIONAL_PREDICATES = frozenset(
    {"employer", "worksfor", "works_for", "spouse", "residence", "homelocation", "home_location"}
)
```

"Functional" answers a single question: *does this predicate allow at most one simultaneously-active value for a given subject?* If yes, a new binding supersedes the old (SCD-2 close + `fact_conflict` review). That is purely a **cardinality-and-supersession** property. It says nothing about the direction of the edge or whether the other party is equally bound.

"Symmetric" (and its asymmetric cousin, "has a named inverse") answers a completely different question: *if X.P → Y is true, is there a reciprocal fact — either Y.P → X (symmetric) or Y.Q → X with a different predicate Q (asymmetric inverse) — that must also be true?* The pipeline has no mechanism for this. The `EXTRACTION_SCHEMA` in `prompt.py` has no `inverse_predicate` field, no `symmetric` flag, and the system prompt (lines 18–133) never instructs the model to emit a reciprocal edge. The worked example closest to the pattern ("owns decomposes into two facts", lines 76–81) decomposes an owner-edge plus an animal-attribute fact, but the second fact is an *attribute* on the animal, not a reverse ownership edge on the animal.

Crucially, `spouse` appears in *both* the `FUNCTIONAL_PREDICATES` set and belongs to the symmetric relation category — which means the system correctly limits Me to one current spouse but still produces only a single directed edge (Me.spouse → Jordan), leaving Jordan's side of the marriage entirely unrepresented. For *asymmetric* relations such as `worksFor` / `employs` the gap is even sharper: the object party's inverse predicate has a *different name* (`employs` vs `worksFor`, `child_of` vs `parent_of`), so not only is the inverse edge absent — it is absent *and* its correct predicate name is never even constructed.

### Why dropping the object-person mention compounds the predicate gap

The lapse has two layers. Layer one: the object party (Celine, in the original example) is frequently extracted as a bare `value_json` string rather than as a `mentions` entry with `{name, kind, surface_text}`. Without a mention, there is no entity node in the graph; without an entity node, `object_entity_ref` cannot point to a resolved row; without a resolved row, no inverse fact can ever be attached to that party's fact stream. Layer two: even when the object party *is* correctly mentioned, the pipeline emits no reciprocal edge.

The severity of layer one varies by relation type. For symmetric relations the missing inverse edge has the *same predicate* — so a single-fact gap is recoverable if the object person is at least mentioned and the prompt later learns to emit the reverse edge using the same predicate name. For asymmetric relations the inverse predicate is *different*: a note that says "Sam's boss is Dana" and the system emits only `Sam.worksFor → Dana` leaves the system unable to infer `Dana.manages → Sam` even in principle without knowing that `worksFor` inverts to `manages`. The inverse-predicate gap is therefore hardest to close for asymmetric relations, because naming it requires relational ontology the prompt currently does not carry.

The system prompt's canonical predicate guidance (lines 47–57 of `prompt.py`) lists `worksFor`, `spouse`, and `homeLocation` as preferred spellings but provides no symmetric or inverse annotations. The `EXTRACTION_SCHEMA` (lines 142–240) has no structural slot for a reverse predicate. The `ANALYSIS.md` "Facts" section (the property-graph grammar) records that `relationship`-kind facts set `object_entity_ref` but says nothing about reciprocal edges. This is the current state: the design is silent on the distinction, not ambiguous about it.

---

## Relation-Type Reference Table

| Relation | Class | Canonical predicate(s) in prompt | Inverse / symmetric predicate | What "mutual status" should look like |
|---|---|---|---|---|
| Spouse / marriage | **Symmetric** | `spouse` | `spouse` (same) | Both X.spouse → Y and Y.spouse → X; functional on each side |
| Sibling | **Symmetric** | `sibling` (coined snake_case) | `sibling` (same) | X.sibling → Y and Y.sibling → X |
| Close friend | **Symmetric** | `knows` / `friend` (coined) | same | X.friend → Y and Y.friend → X |
| Co-founder | **Symmetric** | `co_founder` (coined) | same | X.co_founder → Y and Y.co_founder → X |
| Business partner | **Symmetric** | `business_partner` (coined) | same | X.business_partner → Y and Y.business_partner → X |
| Bandmate / team member | **Symmetric** | `bandmate` (coined) | same | X.bandmate → Y and Y.bandmate → X |
| Parent / child | **Asymmetric** | `parent_of` / `child_of` (coined) | `child_of` ↔ `parent_of` | X.parent_of → Y implies Y.child_of → X |
| Employer / employee | **Asymmetric** | `worksFor` (for employee side) | `employs` (for employer side) | Marcus.worksFor → Acme implies Acme.employs → Marcus |
| Doctor / patient | **Asymmetric** | `treatedBy` / `hasTreated` (coined) | `hasTreated` ↔ `treatedBy` | Patient.treatedBy → Dr. X implies Dr. X.hasTreated → Patient |
| Landlord / tenant | **Asymmetric** | `landlord_of` / `tenant_of` (coined) | `tenant_of` ↔ `landlord_of` | Landlord.landlord_of → Tenant implies Tenant.tenant_of → Landlord |
| Mentor / mentee | **Asymmetric** | `mentors` / `mentee_of` (coined) | `mentee_of` ↔ `mentors` | Mentor.mentors → Mentee implies Mentee.mentee_of → Mentor |
| Manager / report | **Asymmetric** | `manages` / `reportsTo` (coined) | `reportsTo` ↔ `manages` | Dana.manages → Sam implies Sam.reportsTo → Dana |

---

## Ten Test Cases

### Case 1 — Spouse (Symmetric): first-person subject

**Note:** `"Jeff is married to Celine Hopkins."`

**relation_type:** Symmetric

**likely_current_extraction (the lapse):**

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"}
  ],
  "facts": [
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
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

*Lapse: Celine has no mention entry; `object_entity_ref` is null (bare string in `value_json` instead); no reciprocal edge.*

**expected_extraction:**

```json
{
  "mentions": [
    {"name": "Jeff",   "kind": "Person", "surface_text": "Jeff"},
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

*The inverse predicate is identical (`spouse`) because the relation is symmetric. Both Jeff and Celine are functional on `spouse`, so each edge participates in supersession.*

**why_it_matters:** The `spouse` predicate is in `FUNCTIONAL_PREDICATES`, meaning the system enforces "one current spouse" — but only for the subject. Celine's side of the marriage is invisible to the graph; a query for Celine's current spouse returns nothing even though the information is in the corpus.

---

### Case 2 — Sibling (Symmetric): third-person, neither is Me

**Note:** `"Tom and his sister Rosa grew up in Phoenix."`

**relation_type:** Symmetric

**likely_current_extraction (the lapse):**

```json
{
  "mentions": [
    {"name": "Tom",  "kind": "Person", "surface_text": "Tom"},
    {"name": "Rosa", "kind": "Person", "surface_text": "Rosa"}
  ],
  "facts": [
    {
      "predicate": "sibling",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Tom's sister is Rosa.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Tom",
      "object_entity_ref": "Rosa",
      "temporal": null,
      "domain": "general",
      "confidence": 0.88
    }
  ]
}
```

*Lapse: both mentions present (better than Case 1), but only one directed edge. The symmetric edge Rosa.sibling → Tom is absent.*

**expected_extraction:**

```json
{
  "mentions": [
    {"name": "Tom",  "kind": "Person", "surface_text": "Tom"},
    {"name": "Rosa", "kind": "Person", "surface_text": "Rosa"}
  ],
  "facts": [
    {
      "predicate": "sibling",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Tom's sibling is Rosa.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Tom",
      "object_entity_ref": "Rosa",
      "temporal": null,
      "domain": "general",
      "confidence": 0.88
    },
    {
      "predicate": "sibling",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Rosa's sibling is Tom.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Rosa",
      "object_entity_ref": "Tom",
      "temporal": null,
      "domain": "general",
      "confidence": 0.88
    }
  ]
}
```

*Inverse predicate is `sibling` (same). Since `sibling` is not in `FUNCTIONAL_PREDICATES`, both edges accumulate without supersession — which is correct: a person can have multiple siblings.*

**why_it_matters:** Without the reciprocal edge, a future note mentioning "Rosa and her brother" cannot resolve "her brother" via the graph to Tom. Alias resolution for kinship terms depends on relationship facts being bidirectional (ANALYSIS.md "Role references [decided: via relationship facts]", paragraph on `dentist_of`/`employer`).

---

### Case 3 — Friend (Symmetric): first-person subject (Me)

**Note:** `"My sister Dana married Luis last Saturday."`

**relation_type:** Symmetric (marriage)

**likely_current_extraction (the lapse):**

```json
{
  "mentions": [
    {"name": "Me",   "kind": "Person", "surface_text": "My"},
    {"name": "Dana", "kind": "Person", "surface_text": "Dana"},
    {"name": "Luis", "kind": "Person", "surface_text": "Luis"}
  ],
  "facts": [
    {
      "predicate": "sibling",
      "qualifier": "",
      "kind": "relationship",
      "statement": "My sister is Dana.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Me",
      "object_entity_ref": "Dana",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Dana's spouse is Luis.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Dana",
      "object_entity_ref": "Luis",
      "temporal": {"phrase": "last Saturday", "resolved_start": "2026-06-06T00:00:00-05:00", "resolved_end": null, "precision": "day"},
      "domain": "general",
      "confidence": 0.92
    }
  ]
}
```

*Lapse: Me.sibling → Dana has no reciprocal; Dana.spouse → Luis has no reciprocal from Luis.*

**expected_extraction:**

```json
{
  "mentions": [
    {"name": "Me",   "kind": "Person", "surface_text": "My"},
    {"name": "Dana", "kind": "Person", "surface_text": "Dana"},
    {"name": "Luis", "kind": "Person", "surface_text": "Luis"}
  ],
  "facts": [
    {
      "predicate": "sibling",
      "qualifier": "",
      "kind": "relationship",
      "statement": "My sibling is Dana.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Me",
      "object_entity_ref": "Dana",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "sibling",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Dana's sibling is Me.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Dana",
      "object_entity_ref": "Me",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Dana's spouse is Luis.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Dana",
      "object_entity_ref": "Luis",
      "temporal": {"phrase": "last Saturday", "resolved_start": "2026-06-06T00:00:00-05:00", "resolved_end": null, "precision": "day"},
      "domain": "general",
      "confidence": 0.92
    },
    {
      "predicate": "spouse",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Luis's spouse is Dana.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Luis",
      "object_entity_ref": "Dana",
      "temporal": {"phrase": "last Saturday", "resolved_start": "2026-06-06T00:00:00-05:00", "resolved_end": null, "precision": "day"},
      "domain": "general",
      "confidence": 0.92
    }
  ]
}
```

*Two symmetric relations in one note; both require two edges each. The note mentions Me, so the sibling's reciprocal points `object_entity_ref: "Me"`, which is the canonical first-person entity.*

**why_it_matters:** A note containing multiple co-present relationships multiplies the missing-edge count. In a mixed note like this, four facts should exist but only two are emitted — a 50% coverage gap within a single capture.

---

### Case 4 — Parent/Child (Asymmetric): third-person, inverse predicate differs

**Note:** `"Jeff is Sam's father."`

**relation_type:** Asymmetric (parent_of ↔ child_of)

**likely_current_extraction (the lapse):**

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Sam",  "kind": "Person", "surface_text": "Sam"}
  ],
  "facts": [
    {
      "predicate": "parent_of",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff is Sam's father.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Sam",
      "temporal": null,
      "domain": "general",
      "confidence": 0.95
    }
  ]
}
```

*Lapse: Sam.child_of → Jeff is absent. The inverse predicate is not `parent_of` — it is `child_of` — so the system cannot derive it by reflection alone.*

**expected_extraction:**

```json
{
  "mentions": [
    {"name": "Jeff", "kind": "Person", "surface_text": "Jeff"},
    {"name": "Sam",  "kind": "Person", "surface_text": "Sam"}
  ],
  "facts": [
    {
      "predicate": "parent_of",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Jeff is Sam's parent.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Jeff",
      "object_entity_ref": "Sam",
      "temporal": null,
      "domain": "general",
      "confidence": 0.95
    },
    {
      "predicate": "child_of",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Sam is Jeff's child.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Sam",
      "object_entity_ref": "Jeff",
      "temporal": null,
      "domain": "general",
      "confidence": 0.95
    }
  ]
}
```

*The inverse predicate is `child_of`, not `parent_of`. The extractor must know the predicate pair, not just reflect the edge. `child_of` is not in `FUNCTIONAL_PREDICATES` (a person can have multiple parents), so it accumulates.*

**why_it_matters:** This is the sharpest case of the inverse-predicate gap. The pipeline could not derive `child_of` by reversing `parent_of` without explicit relational ontology. If a future note says "Sam's dad is Jeff" and tries to match it against the graph, it emits `child_of → Jeff` from Sam's perspective, but there is nothing to refresh against — the Sam.child_of fact does not exist. A new, orphaned fact is inserted instead of a refresh.

---

### Case 5 — Employer/Employee (Asymmetric): third-person, existing scenario pattern

**Note:** `"Globex hired Marcus last month."`

**relation_type:** Asymmetric (employs ↔ worksFor)

**likely_current_extraction (the lapse):**

```json
{
  "mentions": [
    {"name": "Marcus", "kind": "Person",       "surface_text": "Marcus"},
    {"name": "Globex", "kind": "Organization", "surface_text": "Globex"}
  ],
  "facts": [
    {
      "predicate": "worksFor",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Marcus works for Globex.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Marcus",
      "object_entity_ref": "Globex",
      "temporal": {"phrase": "last month", "resolved_start": "2026-05-01T00:00:00-06:00", "resolved_end": null, "precision": "month"},
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

*Lapse: Globex.employs → Marcus is absent. worksFor is in `FUNCTIONAL_PREDICATES` (one employer per person), but the reciprocal `employs` (which is not functional — an employer has many employees) is never emitted.*

**expected_extraction:**

```json
{
  "mentions": [
    {"name": "Marcus", "kind": "Person",       "surface_text": "Marcus"},
    {"name": "Globex", "kind": "Organization", "surface_text": "Globex"}
  ],
  "facts": [
    {
      "predicate": "worksFor",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Marcus works for Globex.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Marcus",
      "object_entity_ref": "Globex",
      "temporal": {"phrase": "last month", "resolved_start": "2026-05-01T00:00:00-06:00", "resolved_end": null, "precision": "month"},
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "employs",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Globex employs Marcus.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Globex",
      "object_entity_ref": "Marcus",
      "temporal": {"phrase": "last month", "resolved_start": "2026-05-01T00:00:00-06:00", "resolved_end": null, "precision": "month"},
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

*`employs` is the named inverse of `worksFor`. It is NOT in `FUNCTIONAL_PREDICATES` (Globex employs many people), so it accumulates. Note that a later "Globex also hired Priya" adds a second Globex.employs → Priya edge without conflict — correct behavior for a non-functional predicate.*

**why_it_matters:** This is the highest-frequency real-world asymmetric pattern. Role-reference resolution (ANALYSIS.md "Role references via relationship facts") needs both directions: "my boss" resolves via `reportsTo`/`manages`, "my employer" resolves via `worksFor`. The employer's-eye-view (`employs`) is equally needed for queries like "who works at Globex?"

---

### Case 6 — Doctor/Patient (Asymmetric): health domain, asymmetric with named inverse

**Note:** `"Dr. Patel has been treating Elena for her thyroid condition."`

**relation_type:** Asymmetric (hasTreated ↔ treatedBy)

**likely_current_extraction (the lapse):**

```json
{
  "mentions": [
    {"name": "Dr. Patel", "kind": "Person", "surface_text": "Dr. Patel"},
    {"name": "Elena",     "kind": "Person", "surface_text": "Elena"}
  ],
  "facts": [
    {
      "predicate": "hasTreated",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Dr. Patel has been treating Elena.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Dr. Patel",
      "object_entity_ref": "Elena",
      "temporal": null,
      "domain": "health",
      "confidence": 0.88
    }
  ]
}
```

*Lapse: Elena.treatedBy → Dr. Patel is absent. The patient-centric view — which is often the primary query angle — is missing.*

**expected_extraction:**

```json
{
  "mentions": [
    {"name": "Dr. Patel", "kind": "Person", "surface_text": "Dr. Patel"},
    {"name": "Elena",     "kind": "Person", "surface_text": "Elena"}
  ],
  "facts": [
    {
      "predicate": "hasTreated",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Dr. Patel has been treating Elena.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Dr. Patel",
      "object_entity_ref": "Elena",
      "temporal": null,
      "domain": "health",
      "confidence": 0.88
    },
    {
      "predicate": "treatedBy",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Elena is treated by Dr. Patel.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Elena",
      "object_entity_ref": "Dr. Patel",
      "temporal": null,
      "domain": "health",
      "confidence": 0.88
    }
  ]
}
```

*Inverse predicate `treatedBy` differs from `hasTreated`. Neither is in `FUNCTIONAL_PREDICATES` (a doctor treats many patients; a patient may have multiple providers), so both accumulate.*

**why_it_matters:** Health data is the most privacy-sensitive domain. In Phase 7 (intake-link subjects), Dr. Patel's entity may belong to a different security subject than Elena's; the bidirectional edge is the linkage that makes "Elena's current providers" queryable under Elena's subject scope without a cross-domain join. Missing it is both a query failure and a future RLS design pressure.

---

### Case 7 — Landlord/Tenant (Asymmetric): location domain, non-Me third-person pair

**Note:** `"Marco rents an apartment from the Nguyen family."`

**relation_type:** Asymmetric (landlord_of ↔ tenant_of)

**likely_current_extraction (the lapse):**

```json
{
  "mentions": [
    {"name": "Marco",         "kind": "Person", "surface_text": "Marco"},
    {"name": "Nguyen family", "kind": "Person", "surface_text": "the Nguyen family"}
  ],
  "facts": [
    {
      "predicate": "tenant_of",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Marco is a tenant of the Nguyen family.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Marco",
      "object_entity_ref": "Nguyen family",
      "temporal": null,
      "domain": "location",
      "confidence": 0.85
    }
  ]
}
```

*Lapse: Nguyen family.landlord_of → Marco is absent. The landlord's-side fact never exists.*

**expected_extraction:**

```json
{
  "mentions": [
    {"name": "Marco",         "kind": "Person", "surface_text": "Marco"},
    {"name": "Nguyen family", "kind": "Person", "surface_text": "the Nguyen family"}
  ],
  "facts": [
    {
      "predicate": "tenant_of",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Marco is a tenant of the Nguyen family.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Marco",
      "object_entity_ref": "Nguyen family",
      "temporal": null,
      "domain": "location",
      "confidence": 0.85
    },
    {
      "predicate": "landlord_of",
      "qualifier": "",
      "kind": "relationship",
      "statement": "The Nguyen family is Marco's landlord.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Nguyen family",
      "object_entity_ref": "Marco",
      "temporal": null,
      "domain": "location",
      "confidence": 0.85
    }
  ]
}
```

*`landlord_of` is the named inverse of `tenant_of`. Neither is functional. The `surface_text` on the Nguyen family mention uses "the Nguyen family" verbatim, matching the prompt rule that surface_text is copied from the note.*

**why_it_matters:** Location is a sensitive domain. A future query "who does the Nguyen family rent to?" can only be answered via the landlord-side edge. Without it, only tenant-perspective queries succeed. This matters for the wiki's relationship summaries, which would show the Nguyen family as an entity with no outbound rental relationships.

---

### Case 8 — Mentor/Mentee (Asymmetric): first-person Me is the object (not subject)

**Note:** `"Kenji has been mentoring me since I joined the team."`

**relation_type:** Asymmetric (mentors ↔ mentee_of)

**likely_current_extraction (the lapse):**

```json
{
  "mentions": [
    {"name": "Kenji", "kind": "Person", "surface_text": "Kenji"},
    {"name": "Me",    "kind": "Person", "surface_text": "me"}
  ],
  "facts": [
    {
      "predicate": "mentors",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Kenji mentors Me.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Kenji",
      "object_entity_ref": "Me",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

*Lapse: Me.mentee_of → Kenji is absent. This is a case where Me is the object, not the subject — the note is first-person but the grammatical subject is Kenji. The inverse edge would be on Me, the canonical "Me" entity.*

**expected_extraction:**

```json
{
  "mentions": [
    {"name": "Kenji", "kind": "Person", "surface_text": "Kenji"},
    {"name": "Me",    "kind": "Person", "surface_text": "me"}
  ],
  "facts": [
    {
      "predicate": "mentors",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Kenji mentors Me.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Kenji",
      "object_entity_ref": "Me",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "mentee_of",
      "qualifier": "",
      "kind": "relationship",
      "statement": "I am Kenji's mentee.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Me",
      "object_entity_ref": "Kenji",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

*Inverse predicate is `mentee_of`, different from `mentors`. The `entity_ref` on the inverse is `"Me"` — the canonical first-person entity — and `object_entity_ref` is `"Kenji"`. This is one of the cases where the lapse is hardest to notice: Me is correctly identified in both directions, but the Me-as-subject inverse edge is never emitted.*

**why_it_matters:** Me's fact stream is the highest-value stream in the graph — it is the implicit center of the graph per ANALYSIS.md ("Me entity hard-linked to the owner subject row, the implicit center of the graph"). Any fact that should appear on Me's stream but instead lives only on a third party's stream is less findable and less composable with other Me-facts.

---

### Case 9 — Manager/Report (Asymmetric): third-person workplace hierarchy

**Note:** `"Dana runs the platform team and Sam reports to her."`

**relation_type:** Asymmetric (manages ↔ reportsTo)

**likely_current_extraction (the lapse):**

```json
{
  "mentions": [
    {"name": "Dana", "kind": "Person", "surface_text": "Dana"},
    {"name": "Sam",  "kind": "Person", "surface_text": "Sam"}
  ],
  "facts": [
    {
      "predicate": "reportsTo",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Sam reports to Dana.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Sam",
      "object_entity_ref": "Dana",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

*Lapse: Dana.manages → Sam is absent. The system extracts the report's perspective (which is natural from "Sam reports to") but misses the manager's edge.*

**expected_extraction:**

```json
{
  "mentions": [
    {"name": "Dana", "kind": "Person", "surface_text": "Dana"},
    {"name": "Sam",  "kind": "Person", "surface_text": "Sam"}
  ],
  "facts": [
    {
      "predicate": "reportsTo",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Sam reports to Dana.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Sam",
      "object_entity_ref": "Dana",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "manages",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Dana manages Sam.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Dana",
      "object_entity_ref": "Sam",
      "temporal": null,
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

*`manages` is the named inverse of `reportsTo`. Neither is in `FUNCTIONAL_PREDICATES` — Dana can manage multiple reports, Sam could have multiple managers (matrix org). Both accumulate. Note the earlier part of the note ("Dana runs the platform team") could also yield a `manages` fact even without the `reportsTo` clause — the predicate pair must be consistent regardless of which clause triggers extraction.*

**why_it_matters:** Role-reference resolution (ANALYSIS.md "my boss" / "my manager") requires the `manages` edge to be present on the manager's entity. If only `reportsTo` is extracted, a query for "who manages Sam?" must traverse the graph backwards — a direction the current schema-less property graph does not natively support without a full-scan join on `object_entity_ref`.

---

### Case 10 — Co-Founder (Symmetric): business relationship, neither party is Me

**Note:** `"Alicia and Ben co-founded Meridian Labs together back in 2018."`

**relation_type:** Symmetric

**likely_current_extraction (the lapse):**

```json
{
  "mentions": [
    {"name": "Alicia",        "kind": "Person",       "surface_text": "Alicia"},
    {"name": "Ben",           "kind": "Person",       "surface_text": "Ben"},
    {"name": "Meridian Labs", "kind": "Organization", "surface_text": "Meridian Labs"}
  ],
  "facts": [
    {
      "predicate": "co_founder",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Alicia is a co-founder of Meridian Labs.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Alicia",
      "object_entity_ref": "Meridian Labs",
      "temporal": {"phrase": "back in 2018", "resolved_start": "2018-01-01T00:00:00Z", "resolved_end": null, "precision": "year"},
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "co_founder",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Ben is a co-founder of Meridian Labs.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Ben",
      "object_entity_ref": "Meridian Labs",
      "temporal": {"phrase": "back in 2018", "resolved_start": "2018-01-01T00:00:00Z", "resolved_end": null, "precision": "year"},
      "domain": "general",
      "confidence": 0.9
    }
  ]
}
```

*Partial lapse: the model correctly emits both people's edges to the org. But the person-to-person symmetric edge (Alicia.co_founder → Ben and Ben.co_founder → Alicia) is absent. These are the most useful edges for the question "who did Alicia found a company with?"*

**expected_extraction:**

```json
{
  "mentions": [
    {"name": "Alicia",        "kind": "Person",       "surface_text": "Alicia"},
    {"name": "Ben",           "kind": "Person",       "surface_text": "Ben"},
    {"name": "Meridian Labs", "kind": "Organization", "surface_text": "Meridian Labs"}
  ],
  "facts": [
    {
      "predicate": "co_founder",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Alicia is a co-founder of Meridian Labs.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Alicia",
      "object_entity_ref": "Meridian Labs",
      "temporal": {"phrase": "back in 2018", "resolved_start": "2018-01-01T00:00:00Z", "resolved_end": null, "precision": "year"},
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "co_founder",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Ben is a co-founder of Meridian Labs.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Ben",
      "object_entity_ref": "Meridian Labs",
      "temporal": {"phrase": "back in 2018", "resolved_start": "2018-01-01T00:00:00Z", "resolved_end": null, "precision": "year"},
      "domain": "general",
      "confidence": 0.9
    },
    {
      "predicate": "co_founder",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Alicia co-founded Meridian Labs with Ben.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Alicia",
      "object_entity_ref": "Ben",
      "temporal": {"phrase": "back in 2018", "resolved_start": "2018-01-01T00:00:00Z", "resolved_end": null, "precision": "year"},
      "domain": "general",
      "confidence": 0.88
    },
    {
      "predicate": "co_founder",
      "qualifier": "",
      "kind": "relationship",
      "statement": "Ben co-founded Meridian Labs with Alicia.",
      "value_json": null,
      "assertion": "asserted",
      "entity_ref": "Ben",
      "object_entity_ref": "Alicia",
      "temporal": {"phrase": "back in 2018", "resolved_start": "2018-01-01T00:00:00Z", "resolved_end": null, "precision": "year"},
      "domain": "general",
      "confidence": 0.88
    }
  ]
}
```

*`co_founder` is symmetric. The person-to-org edges and the person-to-person symmetric edges are orthogonal facts and should all be emitted. The `MAX_FACTS = 12` cap (prompt.py line 15) is not exceeded here (4 facts). Person-to-person confidence is slightly lower (0.88) because it requires inference from co-presence rather than explicit statement, which the prompt's `confidence` guidance accommodates.*

**why_it_matters:** This case illustrates that even when the extractor gets the object-person mention right (both Alicia and Ben are present), a whole class of edges — person-to-person symmetric ties — is structurally absent. A query for "who are Alicia's business partners or co-founders?" returns nothing unless the person-to-person co_founder edges exist. The org-mediated path (Alicia → Meridian ← Ben) requires a two-hop traversal the property-graph query layer may not support efficiently.

---

## Five-Line Summary

The ten cases cover: **spouse** (symmetric, functional), **sibling** (symmetric, accumulating), **marriage in a mixed-relation note** (two symmetric types in one capture), **parent/child** (asymmetric, sharpest inverse-predicate gap), **employer/employee** (`worksFor`/`employs`, most common asymmetric pattern), **doctor/patient** (asymmetric, health domain, cross-subject RLS implications), **landlord/tenant** (asymmetric, location domain), **mentor/mentee** (asymmetric, Me-as-object edge), **manager/report** (asymmetric, role-reference dependency), and **co-founder** (symmetric person-to-person edge missing even when object-person mention is present). The inverse-predicate gap is sharpest for the four asymmetric cases — **parent/child** (`parent_of` ↔ `child_of`), **employer/employee** (`worksFor` ↔ `employs`), **doctor/patient** (`hasTreated` ↔ `treatedBy`), and **manager/report** (`manages` ↔ `reportsTo`) — because deriving the inverse requires knowing the predicate pair by name, not just reflecting the edge direction; the system prompt's canonical predicate list (prompt.py lines 47–57) and the `EXTRACTION_SCHEMA` provide no mechanism for this. Symmetric cases (spouse, sibling, co-founder) expose the gap too, but their inverse is at least the same predicate string. The `FUNCTIONAL_PREDICATES` set in supersession.py is orthogonal to both classes: it governs cardinality-and-supersession, not directionality, and its presence for `spouse` creates a false sense of completeness — functional on one side is not the same as bidirectional.
