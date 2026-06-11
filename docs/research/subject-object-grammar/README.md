# Subject-as-object extraction lapse — investigation

Research dossiers (no code, prompt, or test changes) on a recurring
`note.extract` failure: when a person occupies any **non-subject** grammatical
role, the extractor tends to drop them — they never become a Person mention or
entity, and any mutual/inverse relationship between the two people is lost.

## The two observed examples

| # | Note | What the model emitted | What's missing |
|---|---|---|---|
| 1 | "Jeff is married to Celine Hopkins" | `Jeff` (Person) + bare `spouse` fact, `object_entity_ref` null | Celine as a Person entity; `Jeff.spouse → Celine`; the mutual `Celine.spouse → Jeff` |
| 2 | "Jeff ate Celine's dinner last night" | `Jeff` (Person) + `ate` event; **`celine` appears in tags** | Celine as a Person entity (she's a possessor, not even the verb's object) |

Both: xai:grok-4.3, Jun 11 2026. Example 2 is the sharper one — see
`_new-example-celine-dinner.md`. It shows the lapse is **not** limited to
relationship objects: any non-subject person is at risk, and the model
demonstrably *noticed* Celine (it tagged her) yet never promoted her to an
entity. The gap is in mention emission, not perception.

## The lapse has two independent layers

1. **Model / prompt layer — the object-person is dropped.** The extractor
   keeps the grammatical subject and discards persons in object, prepositional,
   possessive, by-phrase, or pronoun positions. This is what both screenshots
   show. It is a *prompt* gap; the harness cannot reproduce it because in a
   harness scenario the author plays a perfect model.

2. **Pipeline layer — no mutual / inverse edge exists.** Even given a perfect
   `Jeff.spouse → Celine` edge, there is **no reciprocal, inverse, or symmetric
   edge logic anywhere** in the pipeline (`rg symmetric|inverse|reciprocal` →
   nothing). `spouse` is only a *functional* predicate (supersession.py:24) —
   "one current value", not "symmetric". So "their mutual status" is
   structurally unrepresented, independent of the prompt.

These are separable: fixing the prompt seeds Celine as an entity and a directed
edge; materializing `Celine.spouse → Jeff` (and asymmetric inverses like
`parent_of`/`child_of`) is a distinct pipeline/schema concern.

## The dossiers (30 note→expected cases, schema-accurate)

| File | Angle | Cases |
|---|---|---|
| `A-grammatical-taxonomy.md` | Syntactic constructions: copular, transitive, passive, conjoined-reciprocal, appositive, relative clause, possessive, ditransitive, pronoun-object | 10 |
| `B-relation-type-coverage.md` | Semantic relation types: symmetric (spouse/sibling/co-founder) vs asymmetric-with-inverse (`parent_of`/`child_of`, `worksFor`/`employs`, doctor/patient, `manages`/`reportsTo`) | 10 |
| `C-redteam-prompt-failures.md` | Failure-mode red team: quotes the exact `prompt.py` lines, third-person/multi-entity/pronoun/salience-competition attacks | 10 |

Every example uses the real `EXTRACTION_SCHEMA` (mention `{name, kind,
surface_text}`; fact with `object_entity_ref` etc.), so cases convert directly
into harness scenarios or live-model eval pairs when implementation is greenlit.

## Convergent findings across all three agents

- **No worked example in `prompt.py` shows a Person-to-Person relationship.**
  The `relationship` instruction (prompt.py:75) says "set object_entity_ref"
  but never says the object must *also* be emitted as a mention; the only
  relationship-shaped worked examples are owner-centric (`Me.owns → Bella`) or
  organization-valued. The first-person "Me" anchor is elaborately specified
  (prompt.py:33–36); object-position third parties get no comparable guidance.
- **Schema imposes no cross-check** between `object_entity_ref` and the mentions
  array, so a model can satisfy every literal instruction with the object
  person absent (Agent C, mode 1) — or fold the object into `value_json` as a
  string under "extract less, not more" pressure (prompt.py:132; Agent C, mode 3).
- **Third-person notes** map neither party to "Me", the only role every worked
  example and existing scenario rehearses (Agent C, mode 2).
- **Inverse-predicate gap is sharpest for asymmetric relations** — `parent_of`/
  `child_of`, `worksFor`/`employs`, `manages`/`reportsTo` — which require the
  extractor (or pipeline) to know the predicate *pair* by name, not merely the
  edge direction (Agent B).
- **Pronoun objects** are the irreducible hard case: no surface name to anchor a
  mention without extending the reference-mention rule to third-person pronouns
  (Agents A & C).

## Out of scope (per owner)

No prompt edits or pipeline design proposed here — research and cases only.
Next decision is the owner's: convert the cases into harness `xfail` scenarios,
build a live-model eval set, or use the dossiers to drive a prompt revision.
