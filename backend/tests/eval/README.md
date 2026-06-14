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

### Two modes

- **Intent-level (default)** — extract → integrate → `plan_intent`, asserting the
  produced `IntegrationIntent` + `ArbiterPlan`. No DB; runs anywhere the token is
  set. This is where the MODEL's judgment lives (the production defects were here).
- **DB-mode (`--db`)** — the SAME two Grok calls, then `apply_intent` → COMMIT
  against a throwaway Postgres testcontainer, asserting the **committed graph**:
  dispositions (active vs `pending_review` + a review card), supersession closure
  (the prior edge actually `superseded`), resolve-to-existing (a known mention
  lands on the seeded row, no duplicate), and domain floors (`domain_code` on the
  row). Needs Docker; the graph is truncated between cases.

      JBRAIN_XAI_API_KEY=... uv run python -m tests.eval.run --db

  A case's `seed` block (see `cases.py`) materializes its "known entities" as real
  rows before the run; cases without a `seed` simply run against a fresh graph and
  their resolve/supersede checks no-op.

## Layout

- `corpus/*.json` — the cases (identity/structure, domains/firewall, lifecycle/
  adversarial). Each is a note + a machine-checkable `expect` block (see
  `cases.py` for the schema). A case marked `"advisory": true` reports but never
  fails the gate (its "correct" answer is genuinely debatable).
- `cases.py` — typed loader (`Case`/`expect`, the `seed` block, and the pure
  `DbCommit` committed-state contract). `runner.py` — `run_case` (intent-level;
  injects the owner like production's graph-context) and `run_case_db` (full
  chain → committed graph → `DbCommit`). `assertions.py` — the pure pass/fail
  engines `check_case` / `check_case_db`, unit-tested in
  `tests/unit/test_eval_assertions{,_db}.py` so the GATE LOGIC itself is verified
  in CI even though the model run is opt-in. The DB wiring (seed → apply → read
  back) is proven faked-Grok in `tests/integration/test_eval_db_runner_pg.py`.

## Adding a case

Drop a JSON object into the right `corpus/*.json`. Reproduce any production miss
here first (a red case), then fix the prompt until it's green — that's how the
corpus grows to cover each new failure class.
