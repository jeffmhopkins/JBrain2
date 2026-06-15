# Fix Options: Issue 1 — Non-Subject Person Extraction

**Issue owner:** Fix-design agent  
**Prompt version in scope:** `note-extract-v4` (prompt.py line 11)  
**Date:** 2026-06-11  
**Scope:** Getting a non-subject person reliably emitted as a `mentions` entry
and, for relationship facts, wired as `object_entity_ref`. Inverse/reciprocal
edge generation is a separate issue and is explicitly out of scope here.

---

## 1. Root-Cause Summary

Five structural gaps in the current prompt conspire to drop non-subject persons.
They are documented in detail in
`docs/research/subject-object-grammar/C-redteam-prompt-failures.md`; this
section restates only what is needed to evaluate each option.

| Gap | Where in code | Why it matters |
|---|---|---|
| **G1** — No positive obligation binding `object_entity_ref` to a mention | prompt.py line 75; extraction.py line 369 | The model can emit a `relationship` fact with a null or orphan `object_entity_ref` and the parser does not warn; the object person is never minted |
| **G2** — Worked examples never show a Person-to-Person relationship | prompt.py lines 114–132 | Both examples use Place or Animal as the object; the model lacks a rehearsed path for Person objects |
| **G3** — `spouse` predicate hint (line 52) is object-free | prompt.py line 52 | The hint teaches the predicate name but shows no edge shape with `object_entity_ref` set |
| **G4** — "Extract less, not more" creates asymmetric compression pressure | prompt.py line 132 | Under salience competition the model folds the object person into `value_json` rather than minting a mention |
| **G5** — No mechanism checks `object_entity_ref` against `mentions` post-parse | extraction.py lines 358–376 | A dangling ref or null ref goes silently to the pipeline; there is no repair or flag |

The two observed failures confirm these gaps:

- `"Jeff is married to Celine Hopkins"` → only `Jeff` in mentions; `spouse`
  fact with `object_entity_ref=null`; Celine folded into `value_json`.
- `"Jeff ate Celine's dinner last night"` → `celine` appears in tags
  (smoking gun: the model saw the token) but was never promoted to a Person
  mention; no `object_entity_ref`.

The tag-smoking-gun case is particularly important for option 3: the model
tokenised and classified Celine correctly (hence the tag) but silently
suppressed the mention, confirming the failure is at the mention-emission
decision, not at entity recognition.

---

## 2. Option A — Prompt Engineering

### How it works

Three targeted additions to `prompt.py` (no structural changes):

**A1 — Explicit non-subject rule in the mentions instruction (after line 36)**

Insert immediately after the "never normalize a reference mention" sentence:

```
Every person named or referred to in ANY grammatical role must be emitted as
a mention — not only the grammatical subject. This includes: the object of a
preposition ("married to Celine", "lives with Marcus"), a possessor
("Celine's dinner" → emit Celine), an appositive ("Jeff's wife, Monica"),
a by-phrase ("hired by Jeff"), and a conjoined subject ("Jeff and Celine
married"). A person who appears only in a tag is not a mention: if the name
appeared in the note, it belongs in mentions.
```

**A2 — New Person-to-Person relationship worked example (after line 132)**

```
Worked example — "Jeff is married to Celine Hopkins." (a Person-to-Person
relationship; both parties must be mentions):
  mentions: [{"name":"Jeff","kind":"Person","surface_text":"Jeff"},
             {"name":"Celine Hopkins","kind":"Person","surface_text":"Celine Hopkins"}]
  fact: {"predicate":"spouse","qualifier":"","kind":"relationship",
         "statement":"Jeff's spouse is Celine Hopkins.","value_json":null,
         "assertion":"asserted","entity_ref":"Jeff",
         "object_entity_ref":"Celine Hopkins","temporal":null,
         "domain":"general","confidence":0.95}
RULE: whenever object_entity_ref names a person, that person MUST appear in
mentions. Never fold a person's name into value_json when they could be
object_entity_ref.
```

**A3 — Reinforce with a possessor/possessive-person case (smoketest for the
tag smoking gun)**

```
Worked example — "Jeff ate Celine's dinner." (possessor is a person; emit
a mention for Celine even if she has no fact):
  mentions: [{"name":"Jeff","kind":"Person","surface_text":"Jeff"},
             {"name":"Celine","kind":"Person","surface_text":"Celine's"}]
  (note surface_text is verbatim from the note — "Celine's" — per the
  surface_text rule; name strips the possessive suffix.)
```

**PROMPT_VERSION bump:** `note-extract-v4` → `note-extract-v5`. Per
CLAUDE.md non-negotiable 7 and the prompt.py module docstring, a meaningful
change to the system prompt requires a version bump and triggers a re-extraction
migration for existing notes.

### Files touched

- `backend/src/jbrain/analysis/prompt.py` — PROMPT_VERSION, SYSTEM_PROMPT
- Migration script to re-run extraction on existing notes at v5 (same PR,
  per CLAUDE.md)

### Pros

- Addresses the root model-reasoning failure directly; few-shot examples are
  the strongest signal available for guiding LLM extraction behaviour.
- A3 directly addresses the tag smoking gun (Celine in tags, not mentions).
- No schema changes; no pipeline changes; no DB migrations beyond re-extraction.
- Cheap to verify: a single "be the model" pass with `scripts/llm-harness.sh
  prompt` will show whether the new text is clear.

### Cons

- Cannot be tested in CI (harness README: "does not test the prompt — only a
  live model exercises that"). Green CI does not prove the prompt works.
- Re-extraction migration cost: every existing note at v4 must be re-run at
  v5 — budgeted but real.
- Model compliance is probabilistic: a sufficiently complex construction
  (Case 10 in the taxonomy — pronoun chain across clause boundary) will still
  fail even with perfect instructions.
- The possessor example (A3) introduces a surface_text convention nuance
  (stripping the possessive "'s") that may confuse the model when the name
  never appears without the suffix.
- "Extract less, not more" (G4) is not counteracted — the model still faces
  compression pressure. A4 could add "A person named in ANY role is never
  trivia; always emit them as a mention." but adds more text.

### Cost / Risk

Effort: **S** (1–2 days). Risk: medium — worked examples have outsized influence
on model output, and a poorly worded example can introduce new failure modes
(e.g., over-eager mention minting for every pronoun).

### Non-negotiable interaction

LLM only via the adapter — prompt.py is the adapter's source of truth; no
provider SDK is touched. Tests land in same PR: add harness scenarios
demonstrating correct object-person output (scenarios authored by hand as "be
the model"; they test the pipeline's handling of good model output, not the
prompt itself). 80% backend coverage gate is met by the scenario additions.

---

## 3. Option B — Schema / Structural Enforcement

### How it works

Two sub-options; they are independent and can be combined.

**B1 — Make `object_entity_ref` required (non-null) for `kind: relationship`
at the schema level**

Change `EXTRACTION_SCHEMA` in `prompt.py`:

```python
# current
"object_entity_ref": {"type": ["string", "null"]},

# proposed (inside the facts schema, conditioned on kind == "relationship"):
# JSON Schema draft-07 "if/then/else" conditional:
"if": {"properties": {"kind": {"const": "relationship"}}},
"then": {"required": ["object_entity_ref"],
         "properties": {"object_entity_ref": {"type": "string", "minLength": 1}}}
```

The LLM adapter already validates output against this schema before calling
`parse_extraction` (per CLAUDE.md non-negotiable 1). A relationship fact with
null `object_entity_ref` would be rejected at the schema-validation layer,
forcing the model to either set a ref or change the kind.

**B2 — Server-side cross-check in `extraction.py`: flag or repair a
relationship fact whose `object_entity_ref` has no matching mention**

In `parse_extraction`, after the mentions list is built and before facts are
processed, build a mention name set:

```python
mention_names = {m.name for m in mentions}
```

Then in the facts loop, for relationship-kind facts:

```python
if kind == "relationship" and object_ref and object_ref not in mention_names:
    # Option B2a — auto-mint a provisional mention:
    mentions.append(ExtractedMention(
        name=object_ref,
        kind="Person",      # conservative default; resolver can correct
        surface_text=object_ref,
    ))
    log.warning("analysis.mention_auto_minted", name=object_ref,
                predicate=predicate, reason="orphan_object_entity_ref")
    mention_names.add(object_ref)

    # Option B2b — route to review inbox instead of auto-minting:
    # (flag fact for human review; do not mint)
```

### Files touched

- `backend/src/jbrain/analysis/prompt.py` — EXTRACTION_SCHEMA (B1)
- `backend/src/jbrain/analysis/extraction.py` — parse_extraction (B2)
- Tests in `backend/tests/unit/test_extraction.py` and new harness scenario

### Pros

**B1:**
- Eliminates null `object_entity_ref` on relationship facts at the schema
  boundary — the adapter rejects the output before parse_extraction sees it.
- Deterministic: works regardless of model or prompt version.

**B2 (auto-mint, B2a):**
- Recovers a person the model correctly named in `object_entity_ref` but
  forgot to include in mentions — the least-bad failure mode.
- Pure Python, fully tested in CI without a live model.
- Does not require a PROMPT_VERSION bump or re-extraction migration.

**B2 (review inbox, B2b):**
- No hallucination risk: human confirms before entity is created.

### Cons

**B1:**
- Addresses only the null-ref case on relationship-kinded facts; it does NOT
  help when the model emits a `state`-kinded fact (kind="state") instead of
  "relationship" and folds the person into `value_json`. This is the observed
  lapse: the `spouse` fact in the smoking-gun case had `kind: "state"`, not
  `"relationship"`. B1 offers zero protection against the model choosing the
  wrong kind.
- The conditional JSON Schema (`if/then/else`) may not be supported by the
  adapter's validator without an upgrade check.

**B2 (auto-mint, B2a):**
- The model may set `object_entity_ref` to a hallucinated or
  incorrectly-resolved string (e.g. "her", "someone", a partial name). Auto-
  minting creates junk entities that the resolver may incorrectly merge later.
  The existing `adv_hallucinated_entity_ref` test exercises a related path;
  auto-minting amplifies that risk.
- `kind="Person"` as the auto-minted default is wrong for Organization or
  Place objects — the model may use `object_entity_ref` for any entity type.
- Does not address the root issue: if the model never sets `object_entity_ref`
  at all (the primary failure mode), B2 is a no-op.

**B2 (review inbox, B2b):**
- High volume at scale: many orphan refs go to review rather than being
  resolved automatically, creating inbox noise.

### Interaction with `adv_hallucinated_entity_ref`

`extraction.py` line 369 already stores `object_entity_ref` as a plain string
with no validation. The existing hallucination red-team paths test what happens
when a ref names a non-existent entity downstream. Auto-minting (B2a) would
change that behaviour: previously a dangling ref went to the entity-resolution
layer which handles it; auto-minting intercepts it earlier, potentially masking
the existing resolver's detection. This interaction must be carefully tested.

### Cost / Risk

**B1:** Effort **S**; Risk: medium (schema change; conditional JSON Schema may
need adapter upgrade; does NOT fix the state-vs-relationship kind confusion).  
**B2a:** Effort **S**; Risk: high (auto-minting hallucinated entities is worse
than the original failure; requires careful guarding).  
**B2b:** Effort **S**; Risk: low (conservative; review inbox is the safe
default).

### Non-negotiable interaction

Tests land in the same PR. B2 is pure pipeline code — testable in CI with
faked LLM calls against crafted payloads. B1 requires checking whether the
adapter's JSON Schema validator supports draft-07 conditionals.

---

## 4. Option C — Deterministic Post-Processing / Validation Net

### How it works

What can code enforce WITHOUT the model — and what it cannot.

**What code CAN enforce:**

**C1 — Tag-based mention promotion (addresses the smoking-gun case directly)**

After `parse_extraction`, compare `tags` to `mentions`. If a tag matches a
name in no mention (case-insensitive, stripping possessives), flag it as a
"possible missed mention" and route to the review inbox. This is not
auto-promotion (the tag "celine" could refer to a place, film, or brand —
the resolver must decide), but it surfaces the gap for human or LLM-review
action.

Implementation in `parse_extraction` (extraction.py), post-mentions loop:

```python
mention_name_lower = {m.name.lower() for m in mentions}
for tag in tags:
    # Tags are already lowercase (extraction.py line 306)
    if tag not in mention_name_lower and len(tag) > 2:
        log.info("analysis.tag_not_in_mentions", tag=tag)
        # route to review inbox via existing review mechanism
```

This is the cheapest possible deterministic signal for the exact smoking-gun
failure. It does not require a prompt change or re-extraction.

**C2 — Cross-check `object_entity_ref` against `mentions` with a hard warning**

Already partly described under B2. As a pure validation (no auto-mint), log a
structured warning when a relationship fact's `object_entity_ref` is non-null
but names no mention. The warning is observable in logs and can be counted as
a metric. This costs nothing downstream and requires no schema change.

```python
if kind == "relationship" and object_ref and object_ref not in mention_names:
    log.warning("analysis.orphan_object_entity_ref",
                object_ref=object_ref, predicate=predicate, entity_ref=entity_ref)
```

**What code CANNOT enforce:**

- **Recovering a person the model never surfaced.** If the model silently
  swallowed "Celine" and never produced any string token for her (no tag, no
  `object_entity_ref`, no `value_json` key), deterministic code has nothing to
  work with. This is the irreducible failure case.
- **Distinguishing a legitimate null `object_entity_ref` from a dropped one.**
  A `state` or `attribute` fact properly has `object_entity_ref=null`; a
  `relationship` fact that the model mislabelled as `state` cannot be detected
  as mislabelled without semantic understanding.
- **Correcting kind confusion.** The model emitting `kind:"state"` with the
  object person in `value_json` is semantically wrong but structurally valid;
  no code can reliably correct it without re-invoking the model.

### The tag smoking-gun as a specific detection path

The "Jeff ate Celine's dinner" failure is uniquely recoverable by C1 because
the model DID produce the token "celine" (as a tag). C1 would have flagged
`tag="celine"` not appearing in `mentions` and routed to review. For the
`"Jeff is married to Celine Hopkins"` case, Celine may OR may not appear in
tags — if she does not (the model suppresses her everywhere), C1 is also a
no-op. C1 is therefore a partial, opportunistic safety net, not a full fix.

### Files touched

- `backend/src/jbrain/analysis/extraction.py` — post-mentions tag comparison
  (C1), orphan-ref warning (C2)
- `backend/tests/unit/test_extraction.py` — unit tests for both

### Pros

- Fully testable in CI without a live model (pure Python logic over parsed
  payload fields).
- No prompt change, no re-extraction migration, no schema change.
- C1 directly addresses the observed smoking-gun failure mode.
- C2 makes the orphan-ref gap observable through metrics/logs before a full
  fix is deployed.

### Cons

- Neither C1 nor C2 repairs anything; they only detect and flag.
- C1 has a low base-rate signal: tags are 3–6 per note, and most tags are
  topical, not person names. The signal-to-noise ratio is low for tags that
  happen to match person names (e.g., tag "jeff" is always in mentions).
- Requires a review-inbox routing mechanism to do anything useful with the
  detection; silent logging alone does not fix the graph.
- Does not address the `kind:"state"` misclassification failure path at all.

### Cost / Risk

Effort: **S** (C1+C2 together < 1 day). Risk: very low. Neither path changes
existing behaviour for correctly-extracted notes.

### Non-negotiable interaction

Tests land in same PR. Pure pipeline logic — LLM is never called. No
PROMPT_VERSION bump needed.

---

## 5. Option D — Verification / Eval Strategy

The harness README is explicit: "does not test the prompt — only a live model
exercises that." This hard constraint shapes every verification approach.

### D1 — "Be the model" harness scenarios (CI-safe, tests pipeline only)

Author new harness scenarios in `backend/tests/harness/scenarios/` covering
the five highest-priority failure constructions:

| Scenario file | Construction | Covers |
|---|---|---|
| `person_to_person_relationship.json` | Copular state ("is married to") | Case 1 / the exact known lapse |
| `possessor_person_mention.json` | Possessive ("Celine's dinner") | The tag smoking gun |
| `appositive_relationship.json` | Appositive ("Jeff's wife, Monica") | Case 5 |
| `passive_voice_person.json` | Passive with by-phrase | Case 3 |
| `conjoined_subject_relationship.json` | "X and Y married" | Case 4 |

Each scenario has the `extraction` field hand-authored to the CORRECT shape
(author plays the model), and the `expect` block asserts:

```jsonc
"entities": [
  {"name": "Celine Hopkins", "kind": "Person", "status": "provisional"}
],
"facts": [
  {"entity": "Jeff", "predicate": "spouse", "kind": "relationship",
   "value_contains": "Celine Hopkins", "status": "active"}
]
```

These scenarios pin the PIPELINE'S behaviour given good model output. They
do NOT prove the prompt produces good output, but they do:

1. Document the expected extraction shape precisely, for whoever tunes the
   model.
2. Provide a regression net: if a pipeline change breaks how a correctly-
   emitted object-person is stored, the scenario catches it.
3. Enable the `xfail` pattern: mark each scenario `"xfail": "object-person
   extraction not yet reliable — Issue 1"` until the prompt fix is validated
   by live-model eval, then remove the `xfail` key when the live eval passes.

**Effort: S.** Scenarios are JSON; no code changes needed.

### D2 — Small live-model eval set (out-of-CI, run before/after prompt change)

Maintain a file at `backend/tests/eval/object_person_recall.py` (or equivalent)
containing 10–15 note strings drawn from the taxonomy test cases in
`docs/research/subject-object-grammar/A-grammatical-taxonomy.md`. Run it
manually against the live model before and after a PROMPT_VERSION bump:

```
uv run python backend/tests/eval/object_person_recall.py --model xai:grok-4.3 \
    --prompt-version note-extract-v5
```

Metric: **object-person recall** — the fraction of notes where every named
non-subject person appears in `mentions` AND is wired as `object_entity_ref`
on the corresponding relationship fact.

Target: ≥ 0.90 recall on the 10 taxonomy cases before shipping a prompt
change. Current baseline is approximately 0.10–0.20 (Celine-case failures on
xai:grok-4.3 are confirmed at 0/2 in session evidence).

This eval is NOT in CI. It is a pre-merge gate run by the developer. It
respects the harness constraint: CI only sees faked-model pipeline tests.

**Effort: M** (2–3 days to build the eval runner, author 15 cases, establish
a baseline). Infrastructure cost: one live-model API call per eval run (~15
tokens per note, negligible cost).

### D3 — "Prompt print" inspection before any prompt change ships

The harness already provides `scripts/llm-harness.sh prompt`. Before any
prompt edit goes into a PR, run:

```
scripts/llm-harness.sh prompt
```

and read the rendered output aloud for the five failure constructions. This
costs zero engineering time but catches wording ambiguities (e.g., "referred
to" being interpreted as "mentioned in the subject position") before they reach
the model.

### D4 — Structured log metric: orphan-ref rate

If C2 (orphan-ref warning) is deployed, the `analysis.orphan_object_entity_ref`
log event becomes a durable quality metric. Before the prompt fix: count events
per 1000 relationship facts. After: confirm the count drops. This is the
closest to an automated regression signal achievable without a live eval runner
in CI.

### Recommended verification sequence

1. **Before writing prompt changes:** run D3 (prompt inspection) against the
   five failure constructions.
2. **In the same PR as the prompt change:** deploy C2 (orphan-ref logging) and
   add harness scenarios (D1) as `xfail`.
3. **After merging:** run D2 (live eval) against the new PROMPT_VERSION.
   If recall ≥ 0.90: remove `xfail` from the harness scenarios in a follow-up
   PR.
4. **Ongoing:** D4 (orphan-ref log metric) as a passive regression signal.

---

## 6. Recommendation

### Phased combination: A + C2 + D1 + D2

A single prompt change (Option A) is the only intervention that addresses the
root cause — the model failing to emit the mention. No pipeline fix (B or C)
can recover a person the model never surfaced, and the state-vs-relationship
kind confusion that drives most of the observed failures is invisible to code.
The options interact in a natural sequence:

**Phase 1 — Immediate, no re-extraction (1–2 days)**

Deploy C2 (orphan-ref warning) and D1 (harness scenarios as `xfail`). No
prompt change, no migration cost. This gives:
- Observability: structured logs count the failure rate today (D4 baseline).
- Pipeline regression net: if a pipeline fix accidentally breaks good-model
  output, the `xfail` scenarios catch it.
- The tag smoking-gun is partially covered by an opportunistic C1 addition;
  route flagged tags to the review inbox.

**Phase 2 — Prompt fix with live validation (3–5 days)**

Author the three prompt additions (A1 + A2 + A3), bump PROMPT_VERSION to
`note-extract-v5`, run D3 (prompt inspection) for all five failure
constructions. Run D2 (live eval) against xai:grok-4.3. If recall ≥ 0.90:
merge. Remove `xfail` from harness scenarios. Schedule re-extraction migration.

**Why NOT B1 (schema conditional):** B1 only covers null `object_entity_ref`
on relationship-kinded facts. The observed failure emits `kind:"state"` — B1
is a no-op for the actual lapse. It also introduces conditional JSON Schema
complexity that may require an adapter upgrade.

**Why NOT B2a (auto-mint):** Auto-minting from an orphan `object_entity_ref`
is riskier than the failure it fixes. When the model hallucinates a ref (the
existing `adv_hallucinated_entity_ref` red-team case), auto-minting silently
creates a junk entity. The review-inbox variant (B2b) is safer but creates
inbox noise; it is superseded by A when the prompt is fixed.

**Why NOT standalone C (validation-only):** C alone is observability, not a
fix. It is valuable as Phase 1 scaffolding but insufficient on its own.

**Summary of phased plan:**

| Phase | Actions | Files | Effort | Risk |
|---|---|---|---|---|
| 1 | C2 orphan-ref warning + C1 tag check + D1 xfail scenarios | extraction.py, 5 scenario JSON files | S | Very low |
| 2 | A1+A2+A3 prompt additions, PROMPT_VERSION bump, D2 live eval | prompt.py, re-extraction migration script | M | Medium |

Both phases land as separate PRs with tests. Phase 1 is safe to merge
immediately; Phase 2 waits for live-eval evidence.

---

## 5-Line Summary

1. **Root cause:** The model folds non-subject persons into `value_json` as strings instead of emitting them as mentions with `object_entity_ref` set; the prompt provides no Person-to-Person worked example and no positive obligation linking relationship object refs to mentions.
2. **Option A (prompt engineering):** Three targeted additions — an explicit non-subject-role rule, a Person-to-Person worked example, and a possessor example — directly address the root cause but require a PROMPT_VERSION bump, re-extraction migration, and can only be verified by live-model eval outside CI.
3. **Options B (schema/structural):** Schema enforcement (B1) is a no-op for the actual lapse (model emits `kind:"state"`, not `"relationship"`); auto-mint (B2a) risks creating hallucinated entities; only C2 (orphan-ref warning) and tag-check (C1) are low-risk deterministic additions, but they detect rather than fix.
4. **Option C+D (validation net + eval strategy):** Harness scenarios (`xfail`) + orphan-ref logs provide a CI-safe regression net and baseline metric; a small out-of-CI live eval set (10–15 taxonomy cases, recall ≥ 0.90 gate) is the only way to prove the prompt fix actually works before removing `xfail` guards.
5. **Recommendation:** Phase 1 — deploy C1+C2 detection and D1 `xfail` scenarios immediately (low risk, no migration); Phase 2 — ship A1+A2+A3 prompt additions with PROMPT_VERSION bump only after D2 live eval confirms recall ≥ 0.90 on the taxonomy test set.
