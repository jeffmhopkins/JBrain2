# Prompt evals — the LLM-in-the-loop loop for `note.extract`

This is how a **prompt** change to `note.extract` is *measured* rather than
guessed. The deterministic harness (`tests/harness/`) scripts a perfect model
and exercises the pipeline; it explicitly **cannot test the prompt** — only a
live model does. These evals fill that gap, and are designed so an agent (e.g.
a future Claude Code session) can drive the whole loop.

Three pieces, two of which need **no API key**:

| Tool | Needs a model? | What it does |
|---|---|---|
| `evals/audit.py` | **No** | Offline self-consistency + closed-set temporal audit of the case set. CI-enforced. |
| be-the-model critic | a model (the agent itself) | An agent acts as the extractor over every case and flags expectations a *faithful* extraction would fail (case bugs / over-strict asserts). |
| `evals/run.py` (`scripts/prompt-eval.sh`) | **Yes** (the configured provider) | Runs the REAL prompt through a REAL model and scores its output. Opt-in, never in CI. |

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

## Cases

One array of cases per `evals/cases/*.json` (all loaded automatically). A case:

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
