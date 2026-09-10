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
       "value_contains": "Boulder", "status": "active", "chained": false,
       "closed": false}
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
- `closed` is `valid_to IS NOT NULL` — whether the fact's interval has an END.
  It is the only column that distinguishes a closed interval from an open one on
  an EDGE, which stores no `value_json` at all; before v3 a scenario reaching for
  that had to assert a word in a payload the write path does not produce.
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
  has no field for `assertion`, `kind`, a LONG-TAIL `qualifier`, or a structured
  `value_json`, so `_tool_calls` drops them: `assertion` is always `asserted`,
  `kind` is derived by `_fact_kind`, and `value_json` is rebuilt from a flattened
  string. A qualifier on one of the five registry predicates declaring a
  `qualifier_vocab` DOES survive, folded into the predicate's dotted path by
  `_predicate` (`name.nickname` + `friends` → `name.nickname.friends`); every
  other qualifier is dropped. `confidence` and the temporal's `resolved_end`
  reach the graph as of v3 — `confidence` composed as a MINIMUM with the engine's
  own span check, so a scripted 1.0 on a fumbled quote still lands at 0.4. The
  dropped fields are still required by the extraction schema and still shape the
  front-half parse, which is why they stay — but an `expect` block must not
  assume they survive. See the gap table below.
- `tool_calls` overrides the synthesiser for one step, `{"entities": [...],
  "facts": [...]}` in the tools' own argument shapes. Author it only when the
  faithful default cannot express the case under test — a deliberately fumbled
  `quote`, an object the model chose to leave as a literal. It is **not** a way
  to make a scenario pass: shaping the stand-in to suit the engine is the one
  thing this harness may not do.

## What the tool surface cannot say

Eight rows, from the original six: **three closed, five accepted**. The table
grew because the old row 4 split when its interval half closed (structured
`value_json` and the missing interval END were never the same problem), and
because one gap was never named at all — an `object` string that silently became
an EDGE. Every one was decided by putting the candidate schema in front of the
live model (`backend/evals/shape_probe.py`, the `fields` suite, through
`/api/debug/tool-probe`; gpt-oss-120b at reasoning low, 12 samples an arm) rather
than by argument. `runner.__doc__` carries the same list next to the code that
hits them.

**The finding that decided four of them, and the one worth carrying out of this
exercise: `required` buys PRESENCE, not MEMBERSHIP.** TOOL_SURFACE R3 is right
that gpt-oss fills every required field — it filled every one of them, on every
sample, in every arm. It fills them with a value it invented. Asked for a fact
`kind` from a six-word list, under an imperative "copy exactly ONE of these six
words, and never any other word", it wrote `residence` ×15, `employment` ×10,
`medical` ×9 — **7 legal in 80**. Asked for an `assertion` from a five-word list:
**0 in 72**. The corollary is the design rule: **the only closed vocabularies a
tool grammar can enforce without a JSON-Schema `enum` (plan constraint 8) are the
JSON types themselves** — `number` and `boolean`. A field whose legal values are
WORDS is not buildable on this box; a field whose value is a number, a boolean or
an ISO date is.

And a boolean is not a way around it, only a different failure. Both were tried:

| boolean | legal | fired | verdict |
|---|---|---|---|
| `negated` ("the note says this is over") | 80/80 | `true` **0** of 80, including every "I finally sold the Civic" | never fires — inert |
| `reading` ("a number off an instrument") | 96/96 | `true` **88** of 96, including "Dana still works at Everlane" | always fires — would make every fact a measurement |

The type is filled perfectly and the JUDGEMENT is not there. One collapses to the
majority class; the other to the other one.

| # | Gap | State | What it costs, and what was measured |
|---|---|---|---|
| 1 | **No `assertion`, only `asserted`** | **accepted** | A negated, questioned, reported or hypothetical fact cannot be stated at all. The future (`expected`) and past-marker (interval-close) normalizers still fire — they are `_upsert_fact`'s own — but nothing else does, and a disposal stated in a LATER note cannot reach the earlier note's fact. String: 0/72 legal. Boolean: 0/80 fired. The channel that survives is `correct_fact` on the owner's reply turn. |
| 2 | **No `kind`** | **accepted** | `_fact_kind` derives it: an object edge is always `relationship`; everything else falls to the registry's declaration, then the subject type's default, then `attribute`. On an undeclared predicate over a `Person`, `measurement` / `state` / `preference` are unsayable. String: 7/80 legal. Boolean: 88/96 false-fires. **Closing it is registry work, not tool work** — declare the predicate and `_fact_kind` reads the declaration. |
| 3 | **No `qualifier`** | **accepted, with a bounded channel** | Where the qualifier named the OBJECT entity (`owns.Civic`) nothing is lost — a non-functional predicate keys on its object. Where it discriminated two scalar facts under one predicate, those facts collide on one identity key. A `qualifier` field filled with prose on **61 of 86** facts ("previous weight 182 lb in March"), and an over-applied qualifier SPLITS a key so nothing supersedes again — worse than the collision. What ships is the dotted path `registry.decompose_predicate` already read and v3 teaches (`name.nickname.friends`), bounded to the five registry predicates declaring a `qualifier_vocab`. Long-tail qualifiers still collide. **The channel is open and unreached**: the harness's perfect model uses it, the live model does not — 0 of 39 nickname facts carried a third segment, and it wrote `has nickname` where the registry declares `name.nickname`, so the real blocker is predicate normalization one layer earlier. |
| 4 | **No structured `value_json`** | **accepted as designed** | `object` is a string, so a literal is stored as `{value}` or `{value, unit}` and anything richer is flattened first. An edge with an object entity stores **no** `value_json` at all. TOOL_SURFACE gap 5's deliberate narrowing: the model is never asked to nest. |
| 5 | **One `when`, no interval end** | **CLOSED (v3 `when_end`)** | A seventh flat scalar, never a nested object, exactly as TOOL_SURFACE gap 4 sketched. What the sketch did not anticipate is that the field needs a HANDLER as much as a schema: the model closes the one genuinely-closed interval in a note nearly every time *and* stamps an end on nearly every other — **53 over-applications in 64 items** ("present", "last week", "unspecified", today's date on a fact the note dated today). `graphwritetools._close_interval` refuses an end that is not a date, has no `when` to close, or does not pass the start's own PERIOD. That last comparison is period-against-period: an instant test would have admitted every "today on a fact dated today" and closed the owner's current address at the end of today. **45 spurious ends in 56 items on the shipping schema, 0 admitted**; 4 of the 7 real ones admitted, the other three refused for arriving with a blank `when`. The two added fields cost nothing: 8.0 facts a turn, same well-formedness as the six-field control. |
| 6 | **No `confidence`** | **CLOSED (v3 `confidence`)** | The **safety** one. A JSON `number`, and the type is the point: the string spelling came back `"high"`/`"low"` every time (0/24 legal), the number spelling 94/94. `self_confidence` is `min(engine span check, model number)`, so it **only ever lowers** — a model claiming 1.0 on an unattested quote still lands at 0.4. The direction that matters for a guard that HOLDS is the false positive, and across 94 facts the live model marked down **zero** legible ones. It under-reports rather than over-reports: across 121 facts it converges on exactly 0.5 on an unreadable line, and 0.5 is not `< LOW_CONFIDENCE`. Naming '0.3 or lower' instead of 'below 0.5' made it more consistent (7 of 10 smudged facts marked down, against 6 of 11) without moving it under the threshold. So it is a backstop for the clearly illegible case, not a calibrated dial. Moving `LOW_CONFIDENCE` to meet it is deliberately not done — it is a live threshold the whole `note.extract` path feeds. |
| 7 | **An `object` string became an EDGE** | **CLOSED (validation, not schema)** | Not one of the original six and worth naming: a literal that happened to equal a resolved surface silently became an edge to that entity — an entity's own nickname became a self-edge, and the display projection then had no name fact to read. The registry already declares which predicates take an edge (`value_shape: ref`), so a declared non-ref predicate takes its object literally. An explicit handle still wins; an undeclared tier-2 predicate keeps the permissive link. |
| 8 | **No arbiter** | accepted | `integrate_note` ran the arbiter; this path does not. Its derivations are simply absent (`derive_kinship_gender`: four `gender` facts that main wrote and this path does not), as are its card kinds — `low_confidence_inference` and `new_predicate` are unreachable, so a `count: 0` spec naming either asserts nothing. |

### What W5 may therefore not delete

An accepted gap is a promise that something else still carries the meaning, and
W5a deletes ~940 lines gated on this corpus. Each accepted gap names its own
survivor in its scenarios' `xfail` strings; collected here:

- **Gap 1 (`assertion`)** — `facts.assertion` and its CHECK,
  `supersession.CURRENT_ASSERTIONS` / `_IRREALIS` and the negated-supersedes arm
  of `decide()`, and `extraction.ASSERTIONS`. The EMR importer and the reply turn
  both still write non-asserted rows, and three read surfaces filter on the
  column.
- **Gap 2 (`kind`)** — `_fact_kind`, the registry's per-predicate `kind`
  declaration, and the `attribute_collision` card, which is the only thing
  standing between an undeclared measurement and a silent overwrite.
- **Gap 3 (`qualifier`)** — `facts.qualifier`, the identity key that includes it,
  and `decompose_predicate` plus the `qualifier_vocab` declarations it reads.
- **Gap 4 (`value_json`)** — `facts.value_json`, `_quantity_value`'s unit split,
  and `supersession.values_equal`'s cross-unit comparison: the EMR importer and
  the projections write and read structured values through the non-model path.
- **Gap 8 (arbiter)** — nothing *the harness covers*. The derivations are a genuine
  loss, recorded in `rel_enumerated_children_fan_out`. But "W5 deleting `arbiter.py`
  is the plan" is now **false as written**, and the reason is outside this corpus:
  W4's EMR half routes the deterministic importer through `arbiter.plan_intent`
  (`ingest/emr/integrate.py`), and `AnalysisPipeline.commit_intent` — the seam both
  that importer and the eval runner write through — calls `plan_to_extraction` and
  `compute_signals` itself. `ArbiterPlan` / `PlannedFact` / `plan_intent` /
  `plan_to_extraction` / `compute_signals` therefore have a live non-model producer.
  What W5a may take is the three helpers only `integrate_note` calls
  (`recover_dropped_fields`, `derive_kinship_gender`, `dedup_intent_facts`) — and only
  once `integrate_note` itself can go, which is its own gate below.

### The gate this corpus does NOT close

The six-gap decision is half of W5a's gate; the other half is D13's per-PR rule, *no
PR removes a producer before its replacement is merged and green*. That half is **not
met**, and no scenario here can show it, because the harness drives the write tools
directly and then settles it itself.

Production is now CLOSER than it was, and still not there. Since S2
(`docs/plans/SETTLE_OWNERSHIP.md`) the conversation does run the settle's tail at the end
of a clean pass — `analysis/clarify.settle_conversation`. Since S3 it releases its own
`conversation` claim there too — so this runner's `sweep_note` + `settle_tail` pair is
exactly what production runs, and what it no longer does is call `settle_note` whole,
which made the harness the one place a conversation stamped `note_analysis` with the
empty title its tool surface has no verb for. One scenario shape this corpus cannot
express: production scopes the sweep's `touched` to every conversation session that read
the same note text and refuses to release at all on three conditions
(SETTLE_OWNERSHIP.md S3), while a harness run is one conversation over one note, so those
gates are exercised in `test_conversation_settle_pg.py` and nowhere here.

What the conversation still does NOT produce,
and `integrate_note` therefore still owns alone: the `note_analysis` stamp (it has no
title or tags verb, and the stamp is unconditional — precondition 3), the
`notes.integration_state = 'integrated'` flip that `queue.backfill_pending_integration`
and the workflow reconciler key on (precondition 4), and the settle's two producer-blind
review-card halves, which deliberately stay in the `settle_note` composition so a second
sweeper cannot delete the analyzer's cards.

Its ledger precondition IS landed (W4c/1): `ConversationWrites.facts` is the
whole-conversation union across both turn paths — the owner's reply turn records through
`analysis/clarify.record_reply_writes` at the same seam the unattended pass records at —
and S3 wired the sweep to it (W4c/2). What the harness cannot show is the production
ledger at all: it unions its outcomes in process, so `mention_ids` are available here
where `NoteConversationRepo.writes()` records none and production's sweep therefore
skips the mention reconcile.

`tests/integration/test_note_converse_pg.py::test_a_finished_pass_settles_the_conversation_and_not_the_note`
pins what remains absent, so this is a red test rather than a rediscovery.

## Known gaps (current xfail guards)

Each is a strict-xfail scenario that flips green — and fails the suite until its
`xfail` key is removed — the day its fix lands. **23 of 75**, down from 26: v3
flipped `hist_retrospective_closes_open_interval`,
`hist_idempotent_retrospective_refresh` and `name_legal_reprojects_canonical`.
Each scenario's own `xfail` string is the authority and now carries its own
measurement and its own "W5 must not delete" line; this table is the index.

| Gap | Scenarios | Root |
|---|---|---|
| **No `assertion` (gap 1)** | `adv_negation_then_reassert`, `adv_standalone_negation_active`, `own_acquire_then_dispose`, `own_dispose_refresh_swallows_negation`, `own_disputed_low_confidence`, `own_reacquire_same_entity`, `own_theft_ends_ownership`, `plan_cancelled`, `rel_reported_secondhand`, `health_diagnosis` | accepted: neither a string nor a boolean field is fillable |
| **No `kind` (gap 2)** | `adv_unit_change_false_conflict`, `health_bp_timeseries`, `health_med_change`, `hist_backdated_measurement_insert`, `hist_dst_boundary_local_day`, `hist_preference_retrospective_still_supersedes`, `adv_value_json_abuse`, `health_low_confidence_ocr_guard` | accepted: declare the predicate; `_fact_kind` already reads the declaration |
| **No `qualifier` (gap 3)** | `health_diagnosis`, `adv_value_json_abuse`, `health_low_confidence_ocr_guard` | accepted for LONG-TAIL predicates; the registry ones ride the dotted path |
| **No structured `value_json` (gap 4)** | `plan_relative_date_resolution`, `own_joint_co_ownership`, `adv_value_json_abuse`, `hist_backdated_measurement_insert` (the unit) | accepted as designed: the model is never asked to nest |
| **No arbiter (gap 8)** | `rel_enumerated_children_fan_out` | `derive_kinship_gender` does not run: 8 facts where main wrote 12 |
| **Cross-subject edge migration** | `own_transfer_subject_cannot_move` | candidate read scopes to one entity; a lone counterparty edge never sees the prior owner's head |
| **Bare-name ambiguity not detected** | `adv_same_first_name_collapses` | the auto-link rule fires on one exact match, so a second entity is never minted and the retro-recheck has nothing to fire on |

`health_low_confidence_ocr_guard` moved roots and is worth calling out, because
it is the **safety** scenario and its old reason is now obsolete. The confidence
channel it was xfailed for is LIVE — `test_note_graph_write_pg.py` proves a
perfectly quoted 0.25 read is held behind a `low_confidence` card with the
confident prior left active. What blocks the scenario is two gaps upstream of the
guard: `medicationRegimen` is declared by no type, so it lands as `attribute` and
the collision routes to `attribute_collision` before `decide()`'s
state-supersession arm — where the low-confidence branch lives — is ever reached;
and its qualifier `antihypertensive` is long-tail, so the two regimens do not
share an identity key at all. Declaring `medicationRegimen` closes both.

The last two rows of the table predate the tool re-point.

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

### What v3 changed in three green scenarios

Recorded on the same terms as the table above.

| Scenario | Changed | Why |
|---|---|---|
| `hist_idempotent_retrospective_refresh` | now GREEN | `when_end` carries the interval's end, so restating closed history refreshes in place instead of reading as a re-open. |
| `hist_retrospective_closes_open_interval` | now GREEN, on a new column | The close is asserted on the snapshot's `closed` (`valid_to IS NOT NULL`) rather than on the word "ended" in a `value_json` an edge never stores. That is the two-axis model's own definition of current, and what the old assertion was reaching for. |
| `name_legal_reprojects_canonical` | now GREEN, asserting the REGISTRY's kind | Two v3 behaviours: `name.nickname`'s audience rides the dotted path, and a declared value predicate takes its object literally so the nickname "Sammy" stays a value instead of becoming an edge back to the entity it names. Its `kind` assertions now name what the registry declares (`name.full` a state, `name.nickname` an attribute) rather than what the extraction scripted — the same class of tightening as the entity-kind canonicalization row above. |

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
