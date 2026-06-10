# LLM-in-the-middle test harness

Claude (or anyone) plays the `note.extract` model by hand: a **scenario**
scripts the exact JSON a perfect model would return for a sequence of notes,
and the harness runs it through the **real** `analyze_note` pipeline —
entity resolution, per-kind supersession, temporal tokens, the domain
ratchet, the review inbox — against real Postgres, then asserts the resulting
graph.

It tests the **deterministic pipeline given good model output**. It does *not*
test the prompt — only a live model exercises that. So a scenario stays valid
across prompt versions, and it pins exactly the behaviour the prompt is being
tuned to produce.

## Run the golden scenarios (part of the normal suite)

```
cd backend
uv run pytest -m integration tests/integration/test_harness_scenarios.py
```

Every `scenarios/*.json` becomes one parametrized case. A scenario with an
`xfail` reason encodes behaviour a known-open bug doesn't satisfy yet; it's
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
  "xfail": "reason — OMIT unless a known-open bug means it can't pass yet",
  "steps": [
    {
      "domain": "general",              // capture domain
      "created_at": "2026-06-10T17:11:00-06:00",  // ISO+offset: reported_at + anchor
      "body": "the note text",
      "extraction": { /* the full note.extract JSON you'd emit as the model */ }
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
- A fact spec lists only the columns it cares about; `value_contains` matches
  anywhere in `value_json` + `statement`, case-insensitively.
- `extraction` must satisfy the real schema (`jbrain.analysis.prompt.
  EXTRACTION_SCHEMA`): every fact needs `predicate, qualifier, kind,
  statement, value_json, assertion, entity_ref, object_entity_ref, temporal,
  domain, confidence`; every mention needs `name, kind, surface_text`. A
  `surface_text` should appear in the note `body` so the citation can anchor.
```

## Known gaps (current xfail guards)

Each is a strict-xfail scenario that flips green — and fails the suite until its
`xfail` key is removed — the day its fix lands. Surfaced by the scenario agents
and the red-team review.

| Gap | Scenarios | Root |
|---|---|---|
| **C1** supersession crosses the domain firewall | `cross_domain_no_leak`, `health_cross_domain_no_leak` | `pipeline._existing_facts` candidate read isn't domain-scoped |
| **Object-blind identity key** — distinct `owns`/edge facts collide; disposal/negation swallowed by idempotent refresh | `own_many_items_collide_on_predicate`, `own_dispose_refresh_swallows_negation`, `own_transfer_subject_cannot_move`, `adv_negation_then_reassert` | identity key + `values_equal` ignore `object_entity_id`; refresh branch never writes `assertion` |
| **Retrospective interval-close** has no in-place path | `hist_retrospective_closes_open_interval` | `decide()` has no close-the-open-interval branch (drops `valid_to` or chains a dup) |
| **H2** low-confidence/OCR health facts auto-supersede | `health_low_confidence_ocr_guard` | `decide()` takes no confidence; no `low_confidence` filing |
| **H1** fact cap is prompt-only | `adv_over_extraction_no_cap` | `parse_extraction` doesn't cap facts; no value_json/statement size guard |
| **Reschedule-to-earlier** doesn't supersede | `plan_reschedule_earlier` | `state` ordering by validity, not newest-instruction (`reported_at`) |
| **Role-reference resolution** ("my dentist") | `rel_role_reference` | entity resolution layers 2-3 unimplemented |
| **Unit-change false conflict** | `adv_unit_change_false_conflict` | `values_equal` compares value_json without unit normalization |
| **Bare-name ambiguity not detected** on repeat | `adv_same_first_name_collapses` | repeated bare first name auto-links instead of filing `ambiguous_mention` |
