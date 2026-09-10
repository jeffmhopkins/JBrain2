# Prompt evals — the LLM-in-the-loop loop for `note.extract`

This is how a **prompt** change to `note.extract` is *measured* rather than
guessed. The deterministic harness (`tests/harness/`) scripts a perfect model
and exercises the pipeline; it explicitly **cannot test the prompt** — only a
live model does. These evals fill that gap, and are designed so an agent (e.g.
a future Claude Code session) can drive the whole loop.

> **Where the code lives.** The scoring *core* and the case *corpus* ship inside
> the package — `jbrain/evals/runner.py` and `jbrain/evals/cases/*.json` — so the
> nightly `eval_run` workflow (Phase-5 Track H·B) scores the live model in
> production. This `backend/evals/` directory is the dev-only CLI glue (`run.py`,
> `audit.py`) that imports that core; it is **not** shipped in the image.

Three pieces, two of which need **no API key**:

| Tool | Needs a model? | What it does |
|---|---|---|
| `evals/audit.py` | **No** | Offline self-consistency + closed-set temporal audit of the case set. CI-enforced. |
| be-the-model critic | a model (the agent itself) | An agent acts as the extractor over every case and flags expectations a *faithful* extraction would fail (case bugs / over-strict asserts). |
| `evals/run.py` (`scripts/prompt-eval.sh`) | **Yes** (the configured provider) | Runs the REAL prompt through a REAL model and scores its output. Opt-in, never in CI. |
| `evals/shape_probe.py` | **Yes** (the live box, via a debug token) | Scores whether the model FILLS a proposed *tool schema* — a different question from whether a prompt's output is right, and the one a new tool surface raises first. Opt-in, never in CI. |

## The loop (what a session should do)

1. **Edit** the prompt (`src/jbrain/analysis/prompt.py`) and/or add cases.
2. **Audit (offline):** `uv run python -m evals.audit` — catches asserted
   names/numbers/phrases not in the note, `absent_person` contradictions, and
   off-by-one closed-set temporal dates. Must be clean (also a CI test).
3. **Critic (be the model, no key):** have an agent read every case, mentally
   extract per the v5 prompt + scorer semantics, and flag only the expectations
   a careful model would fail — i.e. case bugs, not the legitimately-hard cases.
   Fix the flags.
4. **Live run (needs a key):** `scripts/prompt-eval.sh` against the configured
   provider. Copy the report back; failures dump exactly what the model
   returned (mentions / edges / facts / dates) so the prompt can be tuned.
5. Iterate 1–4 until green; deploy.

> **Keys are never committed.** `run.py` reads the provider key from the env
> via `Settings` (`JBRAIN_XAI_API_KEY`, etc.). Pass it inline at call time
> (`JBRAIN_XAI_API_KEY=… scripts/prompt-eval.sh`); it never touches the repo.

## `shape_probe.py` — does the model FILL this tool schema?

A different question from "is the prompt's output right", and the one a new tool
surface raises first. It sends a candidate `.tool` schema to the live box through
`/api/debug/tool-probe` (which never runs a handler) and scores the call that comes
back. Two suites:

```
JBRAIN_DEBUG_TOKEN=... uv run python -m evals.shape_probe shape 20
JBRAIN_DEBUG_TOKEN=... uv run python -m evals.shape_probe fields 12
JBRAIN_DEBUG_TOKEN=... uv run python -m evals.shape_probe fields 12 v4_shipping_eight
```

`shape` (W2) answered the batched-vs-flat question for `assert_fact` — 20/20 both
ways, 7.6 items a turn against 1.0. `fields` (W3/T5) answered what ADDING a field
costs, and produced the finding this file exists to keep:

> **`required` buys PRESENCE, not MEMBERSHIP.** gpt-oss fills every required
> string field, every time, with a value it invented. A fact `kind` described with
> its six words, under "copy exactly ONE of these six words and never any other",
> came back **7 legal in 80** (`residence`, `employment`, `medical`). An
> `assertion` from a five-word list: **0 in 72**. The only closed vocabularies a
> tool grammar can enforce without a JSON-Schema `enum` (which segfaults gpt-oss's
> harmony grammar) are the JSON types — `number` and `boolean` — so a field whose
> legal values are WORDS is not buildable on this box, and one whose value is a
> number or an ISO date is.

So an arm scores three things, not one: **well-formedness** (every required field
non-blank, every quote verbatim), **legality** (is the value in the field's
vocabulary), and **correctness** (right on the one fact that needs it, and left
alone on the ones that do not). The third is what decides a field — an
over-applied `qualifier` splits an identity key so nothing supersedes again, and
an over-applied `confidence` parks a true fact behind a card. Add an arm to
`FIELD_ARMS` and a grader to `GRADERS`; keep the old arms, they are the record.

## Running the live eval

```
scripts/prompt-eval.sh                 # all cases against the configured model
scripts/prompt-eval.sh --strict        # exit 1 if any case fails
scripts/prompt-eval.sh --like tmp_     # one category (name-substring filter)
scripts/prompt-eval.sh --case marriage_copular_object
```

It routes to whatever `JBRAIN_LLM_TASKS` points `note.extract` at, so the same
cases score grok, Claude, or a local model. It parses **with** the note's
anchor, so the score reflects what the app *stores* (model output + the
deterministic backward-date repair) — a green eval means a green app.

## The nightly eval (Track H·B) — production regression signal

Migration 0044 seeds a **nightly** `eval_run` schedule (03:00 UTC, an hour after the
graph sweeps) that scores the whole corpus against the live model and stores an
`EvalRun` (`app.eval_runs`). It is the engine's standard schedule+trigger shape:

- **Params (bound in the pipeline step, not per-fire):** `suite="all"` (the whole
  curated set), `version_label="nightly"`. A scheduled trigger has no payload, so the
  run params live on the step; `EvalRunStore.latest()` then tracks a `nightly`
  time-series.
- **Spend:** each run is gated by `SelfImprovementGate` (default 200k tokens/day, est.
  ~50k/run) and the kill-switch. Disable it from Ops (toggle the `nightly_eval_run`
  trigger or its schedule) or flip the kill-switch — no redeploy.
- **Fire on demand:** the trigger is `manual=true` → an emergency "run now" from the
  Ops Automations surface.
- **What it is / isn't (today):** a *stored regression signal* an operator (or a later
  Loop-2/Loop-4 promotion consumer) reads. Automatic candidate↔baseline comparison and
  alerting are NOT wired here — `PromotionService` gates a promotion off stored runs,
  but no self-edit loop drives it yet (deferred to Phase 6).
- **Double-fire posture:** `eval_run` is non-mutating with no dedup key — a manual fire
  on the same night as the schedule simply stores a second `EvalRun` (harmless; `latest`
  returns the newest). The daily budget is the spend cap regardless of fire count.

## How a candidate is gated (the `{task, safety}` contract)

`runner.eval_run_from_cases` scores **two dimensions per case**, and the promotion gate
(`jbrain/workflow/promotion.py::promotion_decision`) depends on the split — never
collapse it to one number:

- **`task`** = fraction of all checks passed.
- **`safety`** = fraction of the *groundedness-guard* checks passed (the `absent:` /
  `not_person:` prefixes), or `1.0` when a case has none.

A candidate promotes only if it **wins its new case** (`task ≥ PASS_THRESHOLD = 1.0`)
**without regressing task OR safety** on any existing case. A flat score would let a
prompt trade groundedness for task points — exactly what the split forbids.

### Curating a new case (the `new_case` convention)

When a prompt/tool edit fixes a failure mode, add the case that proves it and name it as
the promotion's `new_case`:

1. **Author the fixture** in `jbrain/evals/cases/NN_*.json` (category-prefixed; pick the
   file matching the failure's class, e.g. `30_temporal.json` for a date bug). It is the
   shipped production corpus, not throwaway scaffolding — it must pass `audit` + the
   critic step.
2. **`new_case` = the fixture's `name`.** The candidate must score `task = 1.0` on it.
3. **Ownership:** whoever makes the originating change owns the case for that task class
   — it encodes what "fixed" means, so it lives or dies with the behavior it guards.

> **Worked example:** `marriage_copular_object` (in `00_core.json`) — body "Jeff is
> married to Celine Hopkins." asserts `person_mentions: [Jeff, Celine Hopkins]` and an
> `edges: [{predicate: spouse, object: Celine}]`. A copular-object extraction bug that
> dropped the spouse edge would be gated by making this the `new_case`: the candidate
> must wire the edge (task) without inventing a person (safety) to promote.

## Cases

One array of cases per `jbrain/evals/cases/*.json` (all loaded automatically). A case:

```jsonc
{
  "name": "tmp_yesterday_morning_anchor",   // unique, category-prefixed
  "body": "Refilled the prescription yesterday.",
  "domain": "general",                       // general|health|finance|location
  "created_at": "2026-06-11T08:30:00-06:00", // ISO, MUST carry an offset
  "expect": { /* only the keys below */ }
}
```

### Expectation axes (all objective; assert only what's unambiguously correct)

| Key | Meaning | Match |
|---|---|---|
| `person_mentions` / `mentions` | a person / any-kind entity must appear | name substring-overlaps a mention name |
| `mention_kind` `[{name, kind:[…]}]` | present **and** typed within an allowed family | mention's `kind` ∈ the (generous, case-insensitive) set |
| `not_person` | a token must **not** be typed `Person` (may be a non-person mention) | passes if absent or non-Person |
| `absent_person` | a fabricated human / pure non-entity must not be a mention at all | no mention overlaps it |
| `edges` `[{predicate, object}]` | a relationship wires `object` as an edge object | only `object` is scored (substring) |
| `temporal` `[{phrase, resolved_date}]` | a time phrase resolves to a **local** date | phrase overlaps, date == in the note's tz |
| `value` `[{predicate?, contains}]` | a fact carries a measurement/amount | `value_json`+statement contains the text |

A **wrong expectation false-fails a correct model** — worse than no case. Keep
expectations tight; `mention_kind` families should be generous and always
include the schema.org base (`Person`/`Organization`/`Place`/…). The `audit` +
critic steps exist precisely to catch these before a live run.
