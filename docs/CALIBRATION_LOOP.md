# Calibration loop — LLM-in-the-loop testing for the analysis layers

A closed, repeatable loop for calibrating the three analysis-layer prompts
(`note.extract`, `integrate.note`, `entity.disambiguate`) against the owner's
local model on the box, and for guarding their quality in CI. This is the
testing spine behind the deferred Loop-4 self-edit (docs/ROADMAP.md Phase 6):
the same eval corpora + deterministic scorers a human uses to calibrate a prompt
by hand are what an agent would later optimize against.

## Two tracks (the load-bearing distinction)

| | Calibration track | CI track |
|---|---|---|
| **runs** | owner-triggered, on the box | every PR, in GitHub Actions |
| **model** | real local `gpt-oss-120b` (debug async-job path) | **faked** LLM (`FakeLlmClient`) |
| **purpose** | improve prompts/registry; produce golden transcripts | guard the scorers + plumbing; gate merges |
| **speed** | minutes/case, single-GPU serial | milliseconds, parallel |
| **gate** | informs a `PROMPT_VERSION` bump | `--cov-fail-under=80`, audit, scorer unit tests |

The box NEVER runs in CI and NEVER without owner permission. The calibration
track's *outputs* (recorded model responses) become the CI track's fixtures, so
CI replays real model behavior deterministically.

## The loop

```
   committed cases ──► driver (box, 1-at-a-time async) ──► deterministic scorers
        ▲                                                          │
        │                                                          ▼
   regenerate 10                                            diagnose failure
   targeted cases ◄──── fix (prompt v-bump / registry) ◄──── modes (cluster)
                                                                   │
                                          re-run ─► delta + no-regression ─► gate v-bump
                                                                   │
                                                      record box output ─► CI golden transcript
```

## What already exists (reuse, do not rebuild)

- `jbrain.evals.runner` — `load_cases`, `score_cases`, `eval_run_from_cases`, the
  `CaseResult` shape and the proven case JSON schema (note.extract only today).
- `backend/evals/audit.py` — offline case validator, CI-enforced via
  `test_eval_scoring.py::test_eval_cases_pass_audit`.
- `jbrain.workflow.promotion.promotion_decision` — the two-dimensional
  `{task, safety}` gate (candidate must win its new case with no task OR safety
  regression); `EvalRun` / `FixtureScore`.
- `jbrain.workflow.evalaction` — the nightly `eval_run` action with
  `SelfImprovementGate` (kill-switch + token budget) and `EvalRunStore`.
- The debug async-job path (`/api/debug/complete-async` + `/jobs/{id}`) — the box
  driver that survives the Cloudflare ~100s edge timeout.
- Deterministic oracles: `analysis.intent.validate_intent` (L2 violation codes),
  `analysis.arbiter.plan_intent` + `analysis.weight.{effective_weight,commit_status}`
  (per-kind supersede/accumulate/conflict + review routing),
  `analysis.extraction.{domain_floor,ratchet_domain}` (firewall), and the
  `SchemaRegistry` validators (`validate_value`, `coerce_value`, `normalize_predicate`).

## Gaps this plan closes

1. **No committed cases for `integrate.note` or `entity.disambiguate`** — only
   `note.extract` (325 cases). A prompt change to L2/L3 has no scoring feedback.
2. **No L2/L3 scorers** — the runner scores extraction expectations only.
3. **No box driver in the repo** — calibration was run from scratch scripts.
4. **No render-layer regression guard** — the `value_label` "never empty" + the
   backend/frontend parity the last review caught.

## Per-layer scoring (deterministic — no human in the check)

- **L1 `note.extract`** (exists): the `expect{}` checks + `_GROUNDEDNESS_PREFIXES`
  split. Augment with the no-sentence / no-empty render assertion.
- **L2 `integrate.note`** (new): build an `Extraction` + `graph_context` fixture
  per case; drive `integrate.note`; parse with `parse_intent`; score
  `validate_intent` violation codes plus per-case judgment golds (resolve-existing,
  supersede/accumulate/**conflict**, `cross_subject`, `ambiguous`, never-mint-a-name)
  and a global "no sentence in any `value_json`" check.
- **L3 `entity.disambiguate`** (new): mention + candidate set per case; drive the
  task; score the link decision — `false_link` (chose an id when gold is null) is
  the critical metric, `missed_link` (null when an id was right) is the safe one.
- **E2E** (staged): swap the box model into the `test_integrate_note_pg.py`
  skeleton behind an owner-gated marker; assert the committed graph (status,
  `domain_code` firewall, enum coercion) via the arbiter/RLS oracles.

## Case schemas (mirror the existing note.extract shape)

```jsonc
// integrate case
{ "name": "supersede_employer",
  "note_text": "...", "mentions": [...], "facts": [...],
  "owner": { "id": "...", "name": "...", "facts": [...] }, "others": [...],
  "gold": { "resolve_existing": {"Me": "ent-owner"}, "supersede": {"worksFor": "supersede"},
            "no_supersede": [...], "conflict": [...], "cross_subject": {"Mom": true},
            "ambiguous": [...], "no_mint_name": [...] } }

// disambiguate case
{ "name": "context_link_bob", "mention": "Bob", "kind": "Person", "context": "...",
  "candidates": [{"id": "...", "name": "...", "kind": "...", "summary": "..."}],
  "gold": "ent-bob-reyes" /* or null */ }
```

## Build phases

- **A** — render-layer regression guards (never-empty + backend/frontend parity).
- **B** — L3 eval module (`disambiguate_runner`) + cases + `FakeLlmClient` tests + audit.
- **C** — L2 eval module (`integrate_runner`) + cases + `FakeLlmClient` tests + audit.
- **D** — box calibration drivers committed under `backend/evals/box/` (owner-run),
  one per layer, sharing the async-job client; a README documents the loop + the
  no-box-without-permission and single-GPU-serial rules.
- **E** (staged) — record box outputs as golden transcripts; the E2E box test.

## Constraints (baked into the box driver)

1. Single GPU serializes → one case at a time, zero concurrent probes.
2. Tunnel ~100s edge timeout → async submit/poll jobs.
3. Non-determinism → N samples/case, report a rate, flag high variance.
4. The driver sends the LOCAL prompt/registry → an uncommitted edit is exercised
   before it ships (how note-extract-v22 was validated without deploying).
5. No box call without explicit owner permission; CI is box-free.
