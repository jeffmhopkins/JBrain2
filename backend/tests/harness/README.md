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
