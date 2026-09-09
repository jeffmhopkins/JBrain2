> **Status:** Living · **Last verified:** 2026-09-09

# LLM-in-the-middle test harness

Claude (or anyone) plays the model by hand: a **scenario** scripts the exact
JSON a perfect model would return for a sequence of notes, and the harness runs
it through the **real graph-write path** against real Postgres, then asserts the
resulting graph.

Since W3 that path is the **note conversation's tool loop, compiled down to its
two write tools**. `runner._tool_calls` turns each step's scripted extraction
into the `resolve_entity` / `assert_fact` arguments a faithful agent would send,
and `NoteGraphWriter` (`jbrain.agent.graphwritetools`) executes them: real
resolution, real `commit_facts`, real `supersession.decide()`, real domain floor
and ratchet, real citation anchoring, then ONE `settle_note` over the union of
every call's writes. What is **not** run is the model itself, the `AgentLoop`,
`max_steps`, the budgets, the tool sidecars' schemas, and — since W3 — the
**arbiter**, which the old `integrate_note` path ran and this one does not.

It tests the **deterministic engine given good model output**. It does *not*
test the prompt, the loop, or the tool schemas — only a live model exercises
those. So a scenario stays valid across prompt versions, and it pins exactly the
behaviour the prompt is being tuned to produce.

**The synthesiser is never tuned to the engine.** `_tool_calls` and
`_object_literal` stand in for a *perfect* model, and that is the whole contract:
if a scenario fails, the ENGINE changed. A branch shaped so the engine's dedup
would compare equal (there was one — it re-spelled `{"kg": 80.0}` as `"80.0 kg"`
so two spellings of one weight stayed comparable) hides an engine gap behind a
sympathetic stand-in and is not allowed. Remove the branch and xfail whatever
fails.

## Run the golden scenarios (part of the normal suite)

```
cd backend
uv run pytest -m integration tests/integration/test_harness_scenarios.py
```

Every `scenarios/*.json` becomes one parametrized case. A scenario with an
`xfail` reason encodes behaviour a known-open gap doesn't satisfy yet; it's
`xfail(strict)`, so when the fix lands the case **xpasses and fails the
suite** until someone deletes the `xfail` key — a built-in reminder.

## Interactive "be the model" (ad-hoc, standing DB)

```
scripts/llm-harness.sh up           # throwaway Postgres + migrate
scripts/llm-harness.sh prompt       # print the real system+user prompt to read
scripts/llm-harness.sh run tests/harness/scenarios/relocation_supersession.json
scripts/llm-harness.sh down
```

`prompt` prints exactly what the model sees (including the capture anchor with
its timezone) — the fastest way to spot a prompt ambiguity. `run` applies a
scenario and prints the resulting facts/reviews plus PASS/FAIL.

## Authoring a scenario

A scenario is one JSON file in `scenarios/`:

```jsonc
{
  "name": "short human title",
  "description": "what behaviour this pins and why it matters",
  "xfail": "reason — OMIT unless a known-open gap means it can't pass yet",
  "steps": [
    {
      "domain": "general",              // capture domain
      "created_at": "2026-06-10T17:11:00-06:00",  // ISO+offset: reported_at + anchor
      "body": "the note text",
      "extraction": { /* the full note.extract JSON you'd emit as the model */ },
      "tool_calls": { /* OPTIONAL: the exact tool arguments — see below */ }
    },
    {
      "reanalyze_step": 0,              // OPTIONAL: re-run the pipeline on step 0's note
      "body": "the note text",          // ignored on a re-run (kept for readability)
      "extraction": { /* what the model NOW reads from the same note */ }
    }
  ],
  "expect": {
    "facts": [
      {"entity": "Sarah", "predicate": "homeLocation", "kind": "state",
       "value_contains": "Boulder", "status": "active", "chained": false}
    ],
    "absent_facts": [ {"...": "must match zero facts"} ],
    "review_items": [ {"kind": "fact_conflict", "summary_contains": "homeLocation"} ],
    "entities": [ {"name": "Sarah", "kind": "Person", "status": "provisional"} ]
  }
}
```

Notes on authoring:

- **You are the model.** Resolve every relative time phrase against
  `created_at` yourself and put absolute ISO values in `temporal`; the prompt
  asks the real model to do the same.
- Steps run **in order, sharing the graph** — that's how you test supersession
  (note 1 sets a value, note 2 changes it) and entity linking across notes
  (reuse the same mention `name`).
- `reanalyze_step: <index>` re-runs the pipeline against the note an earlier
  step seeded (0-based) instead of seeding a new one — same note row, same
  `reported_at`, only the scripted extraction differs. That's how you pin
  re-extraction behaviour: dropped keys retract, identical output refreshes
  in place with no review noise, and retraction-triggered chain repair
  restores facts the dropped one had superseded. The step's `body`/`domain`/
  `created_at` are ignored on a re-run.
- A fact spec lists only the columns it cares about; `value_contains` matches
  anywhere in `value_json` + `statement`, case-insensitively. **`statement` is
  in that haystack**, so an assertion aimed at a stored VALUE must not be a
  substring of the sentence the scenario itself supplies — it would then pass
  iff the scenario's own words were stored, which distinguishes nothing.
- `extraction` must satisfy the real schema (`jbrain.analysis.prompt.
  EXTRACTION_SCHEMA`): every fact needs `predicate, qualifier, kind,
  statement, value_json, assertion, entity_ref, object_entity_ref, temporal,
  domain, confidence`; every mention needs `name, kind, surface_text`. A
  `surface_text` should appear in the note `body` so the citation can anchor.
- **Four of those authored fields no longer reach the graph.** The tool surface
  has no field for `assertion`, `kind`, `qualifier` or a structured
  `value_json`, and none for the model's `confidence`, so `_tool_calls` drops
  them: `assertion` is always `asserted`, `kind` is derived by `_fact_kind`,
  `qualifier` is empty, `value_json` is rebuilt from a flattened string, and
  `self_confidence` is always 1.0. They are still required by the extraction
  schema and still shape the front-half parse, which is why they stay — but an
  `expect` block must not assume they survive. See the gap table below.
- `tool_calls` overrides the synthesiser for one step, `{"entities": [...],
  "facts": [...]}` in the tools' own argument shapes. Author it only when the
  faithful default cannot express the case under test — a deliberately fumbled
  `quote`, an object the model chose to leave as a literal. It is **not** a way
  to make a scenario pass: shaping the stand-in to suit the engine is the one
  thing this harness may not do.

## What the tool surface cannot say

Six gaps, all real, all in `assert_fact`. `runner.__doc__` carries the same list
next to the code that hits them.

| # | Gap | What it costs |
|---|---|---|
| 1 | **No `assertion`, only `asserted`** | A negated, questioned, reported or hypothetical fact cannot be stated at all. The future (`expected`) and past-marker (interval-close) normalizers still fire — they are `_upsert_fact`'s own — but nothing else does, and a disposal stated in a LATER note cannot reach the earlier note's fact. |
| 2 | **No `kind`** | `_fact_kind` derives it: an object edge is always `relationship`; everything else falls to the registry's declaration, then the subject type's default, then `attribute`. On an undeclared predicate over a `Person`, `measurement` / `state` / `preference` are unsayable, and asserting `kind: relationship` on an object edge is a tautology. |
| 3 | **No `qualifier`** | Where the qualifier named the OBJECT entity (`owns.Civic`) nothing is lost — a non-functional predicate keys on its object. Where it discriminated two scalar facts under one predicate (two diagnoses, three readings) those facts now collide on one identity key. |
| 4 | **No structured `value_json`** | `object` is a string, so a literal is stored as `{value}` or `{value, unit}` and anything richer is flattened first. An edge with an object entity stores **no** `value_json` at all. There is one `when`, carrying a START, so an interval END cannot be written. |
| 5 | **No `confidence`** | `assert_fact` stamps `self_confidence=1.0`, and `supersession.decide`'s low-confidence guard is keyed on exactly that field — so the guard is unreachable from a tool write and a 0.25 OCR read supersedes a 0.95 prior with no card. The one **safety** gap of the six. |
| 6 | **No arbiter** | `integrate_note` ran the arbiter; this path does not. Its derivations are simply absent (`derive_kinship_gender`: four `gender` facts that main wrote and this path does not), as are its card kinds — `low_confidence_inference` and `new_predicate` are unreachable, so a `count: 0` spec naming either asserts nothing. |

## Known gaps (current xfail guards)

Each is a strict-xfail scenario that flips green — and fails the suite until its
`xfail` key is removed — the day its fix lands. **26 of 75.** Each scenario's own
`xfail` string is the authority; this table is the index.

| Gap | Scenarios | Root |
|---|---|---|
| **No `assertion` (gap 1)** | `adv_negation_then_reassert`, `adv_standalone_negation_active`, `own_acquire_then_dispose`, `own_dispose_refresh_swallows_negation`, `own_disputed_low_confidence`, `own_reacquire_same_entity`, `own_theft_ends_ownership`, `plan_cancelled`, `rel_reported_secondhand`, `health_diagnosis` | `assert_fact` writes `asserted` and has no field for anything else |
| **No `kind` (gap 2)** | `adv_unit_change_false_conflict`, `health_bp_timeseries`, `health_med_change`, `hist_backdated_measurement_insert`, `hist_dst_boundary_local_day`, `hist_preference_retrospective_still_supersedes`, `adv_value_json_abuse` | undeclared predicate + `Person` default ⇒ `attribute`, so a time-series or a clean state supersession becomes an `attribute_collision` |
| **No `qualifier` (gap 3)** | `health_diagnosis`, `adv_value_json_abuse` | two scalar facts under one predicate collide on one identity key |
| **No structured `value_json` (gap 4)** | `plan_relative_date_resolution`, `own_joint_co_ownership`, `adv_value_json_abuse`, `hist_backdated_measurement_insert` (the unit) | a flattened string round-trips through `_quantity_value`; an edge stores none at all |
| **One `when`, no interval end (gap 4)** | `hist_retrospective_closes_open_interval`, `hist_idempotent_retrospective_refresh` | a closed interval cannot be written, so a restatement of closed history reads as a re-open |
| **No `confidence` (gap 5)** | `health_low_confidence_ocr_guard` | **safety**: the low-confidence supersession guard is unreachable; a blurry OCR read overwrites a confident prior with no card |
| **No arbiter (gap 6)** | `rel_enumerated_children_fan_out` | `derive_kinship_gender` does not run: 8 facts where main wrote 12 |
| **Literal that equals a resolved name** | `name_legal_reprojects_canonical` | `object` is looked up against the conversation's handles first, so a literal value equal to a resolved name silently becomes an edge |
| **Cross-subject edge migration** | `own_transfer_subject_cannot_move` | candidate read scopes to one entity; a lone counterparty edge never sees the prior owner's head |
| **Bare-name ambiguity not detected** | `adv_same_first_name_collapses` | the auto-link rule fires on one exact match, so a second entity is never minted and the retro-recheck has nothing to fire on |

The last three predate the tool re-point; the rest arrived with it.

## Behaviour the re-point changed that no scenario xfails

Recorded here rather than left to be rediscovered. Each is a real difference
from the `integrate_note` path, judged not worth an xfail because the scenario's
stated purpose survives — but a green in these files is **narrower** than the
same green was before.

| Scenario(s) | Changed | Why it is recorded, not xfailed |
|---|---|---|
| `adv_duplicate_mentions`, `adv_duplicate_property_one_note`, `adv_prompt_injection_body_inert`, `plan_recurring_gym`, `pred_longtail_commits_raw`, `health_appointment_visit_and_followup` | asserted `kind` rewritten from `state`/`measurement`/`preference` to `attribute` (gap 2) | each scenario's purpose is a count, a domain, a pin flag or an RRULE, not the classification. The classification gap itself is owned by the seven xfails above — `health_bp_timeseries` is the one that pins a measurement series. `adv_duplicate_property_one_note`'s `absent_facts` on `kind: measurement` is now unfalsifiable but is fully subsumed by the `count: 1` guard beside it. |
| `loc_home_place_edge_supersedes`, `rel_conjoined_past_employers` | asserted `kind` rewritten `state` → `relationship` | not a weakening: `_fact_kind` returns `relationship` for **every** object edge, by design. It does mean `kind: relationship` on an edge is now a tautology; the supersession these files exist for is still asserted through `status`/`chained`. |
| `own_acquire_keeps_edge`, `own_many_items_qualifier_disambiguates`, `rel_twin_sibling_inverse_materialized`, `plan_recurring_gym` | `qualifier` assertions removed (gap 3) | in the first two the qualifier NAMED the object entity, and a non-functional predicate keys on its object, so the disambiguation still happens — by object, not by qualifier. `own_many_items_qualifier_disambiguates` is therefore a misnomer: read it as "many items do not collide". `twin` and `gym` are genuinely lost discriminators, but nothing collides on their absence. |
| seven `plan_*` / `health_*` files | entity `kind` `appointment` → `Event` | `resolve_entity` canonicalizes the declared word through `_KIND_HINTS`, so the stored kind is the registry spelling. Stricter, not weaker. `adv_value_json_abuse` was the one file this update missed (`device` → `Device`); it is corrected. |
| `i5_inferred_sensitive_holds_for_review` | asserts the OPPOSITE of what its filename says | the I5 hold lived in the arbiter (gap 6) and D2 deliberately removed it: a clear fact commits. The file's own `description` states the inversion and what replaced it (the domain floor, which it now pins). **The filename is stale** and is kept only because two ratified docs cite it by name. |
| `cross_domain_no_leak`, `health_cross_domain_no_leak` | `review_items: [{kind: fact_conflict, count: 0}]` → `[{count: 0}]` | these predicates land as `attribute`, whose collision card is `attribute_collision`, so naming `fact_conflict` made the leak detector unfalsifiable. "No review card of any kind" is both falsifiable and what the firewall actually promises. |

## Two things W3 planned here and did not do

Neither is done; both are open, and neither is covered by anything above.

- **The eval corpora are untouched.** `backend/evals/integrate_runner.py` and
  `evals/integrate_cases/00_core.json` still score the `integrate.note` prompt
  and the `IntegrationIntent` this wave replaces, and
  `tests/unit/test_integrate_eval.py` still runs them in CI. They pass, and they
  measure a path the note conversation no longer takes.
- **No scenario runs a real model against a hostile body.**
  `adv_prompt_injection_body_inert` is a tautology by its own description — the
  harness IS the model, so a scripted extraction that declines to comply proves
  only that the script declines to comply. It is now *more* of a tautology than
  before: the write path is a tool loop, which is a shape hostile text could
  plausibly drive, and nothing here exercises the loop at all.
