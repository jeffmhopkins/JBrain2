# Real-Grok evaluation harness

A graded corpus run through the **real production chain** (extract → integrate →
arbiter) against **real Grok**, asserting graph QUALITY — the things the owner
sees: bare `value_json` (never a sentence), no duplicate/junk entities, correct
resolution / disposition / cross-subject / supersession, and the adversarial
safety set. This is the gate that would have caught the sentence-as-value and
minted-nickname regressions before they reached production.

## Run it (opt-in — costs money, needs the token)

    JBRAIN_XAI_API_KEY=... scripts/grok-eval.sh            # full corpus
    JBRAIN_XAI_API_KEY=... scripts/grok-eval.sh prod-bug   # filter by id substring

Exit code is non-zero if any **non-advisory** case regresses. Run it before
shipping any change to `note.extract` / `integrate.note` / the weight model.

## Layout

- `corpus/*.json` — the cases (identity/structure, domains/firewall, lifecycle/
  adversarial). Each is a note + a machine-checkable `expect` block (see
  `cases.py` for the schema). A case marked `"advisory": true` reports but never
  fails the gate (its "correct" answer is genuinely debatable).
- `cases.py` — typed loader. `runner.py` — runs one case (intent-level; injects
  the owner like production's graph-context). `assertions.py` — the pure
  pass/fail engine, unit-tested in `tests/unit/test_eval_assertions.py` so the
  GATE LOGIC itself is verified in CI even though the model run is opt-in.

## Adding a case

Drop a JSON object into the right `corpus/*.json`. Reproduce any production miss
here first (a red case), then fix the prompt until it's green — that's how the
corpus grows to cover each new failure class.
