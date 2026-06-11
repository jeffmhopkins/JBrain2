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
| **Cross-subject edge migration** — an ownership transfer can't close the prior owner's edge | `own_transfer_subject_cannot_move` | candidate read scopes to one entity; a lone counterparty edge never sees the prior owner's head (disposal itself works now — an assertion flip supersedes — but only when the extraction also emits the negated prior-owner edge) |
| **Bare-name ambiguity not detected** on repeat | `adv_same_first_name_collapses` | the spec's auto-link rule fires (one exact "Zane" match) and extraction emits the identical bare name, so a second entity is never minted and the retro-recheck (which triggers on second-entity creation) has nothing to fire on; catching it needs co-mention-signal disambiguation |
