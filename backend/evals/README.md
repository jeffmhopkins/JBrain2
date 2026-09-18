# Prompt evals

> **Status:** Living · **Last verified:** 2026-09-11

**R4 deleted the `note.extract` loop this directory was built around** — the prompt, its
schema, the curated case corpus (`jbrain/evals/cases/*.json`), the scorer
(`jbrain/evals/runner.py`), the `run.py` CLI, the `audit.py` offline check and
`scripts/prompt-eval.sh` — because they scored a prompt that no longer exists
(`../../docs/plans/AGENT_INGEST_REWRITE.md` §4 ⟲ (6)). The `integrate.note` corpus went
with it for the same reason.

**What replaces it is R5's, and is not built yet:** a `close_reading` eval corpus, plus a
real-model adversarial scenario against a hostile note body driving a tool loop. The
deleted cases are the right input to cut the first from — they are a curated,
audited corpus of notes with objective expectations, and only their OUTPUT shape is stale.
They are recoverable at R4's parent commit.

**What is here now:**

| Tool | Needs a model? | What it does |
|---|---|---|
| `evals/shape_probe.py` | **Yes** (the live box, via a debug token) | Scores whether the model FILLS a proposed *tool schema*, and what it DOES when it cannot settle something. Opt-in, never in CI. |
| `evals/box/` | **Yes** (the live box, via a debug token) | Drives a committed corpus through the box's local model with the same scorers CI uses. One layer left: `disambiguate`. Owner-run only. |

The deterministic harness (`../tests/harness/`) scripts a perfect model and exercises the
write path; it explicitly **cannot test the prompt** — only a live model does. That gap is
what this directory exists for, and R5 is what fills it again.

> **Keys are never committed.** Provider keys come from the env via `Settings`
> (`JBRAIN_XAI_API_KEY`, etc.); pass one inline at call time, never in the repo.

## `shape_probe.py` — does the model FILL this tool schema?

A different question from "is the prompt's output right", and the one a new tool
surface raises first. It sends a candidate `.tool` schema to the live box through
`/api/debug/tool-probe` (which never runs a handler) and scores the call that comes
back. Two suites:

```
JBRAIN_DEBUG_TOKEN=... uv run python -m evals.shape_probe shape 20
JBRAIN_DEBUG_TOKEN=... uv run python -m evals.shape_probe fields 12
JBRAIN_DEBUG_TOKEN=... uv run python -m evals.shape_probe fields 12 v4_shipping_eight
JBRAIN_DEBUG_TOKEN=... uv run python -m evals.shape_probe repeats 20
JBRAIN_DEBUG_TOKEN=... SHAPE_PROBE_DUMP=/tmp/runs uv run python -m evals.shape_probe ask 12
JBRAIN_DEBUG_TOKEN=... SHAPE_PROBE_DUMP=/tmp/runs uv run python -m evals.shape_probe contradict 8
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

## The three behavioural suites (R0 of `AGENT_INGEST_REWRITE.md`)

`shape` and `fields` ask what the model puts in a field. R0 needed two questions those
cannot reach — *does a GRAMMAR fare better than a word list*, and *what does the agent DO
when it cannot settle something* — so the probe grew three suites and a second transport.

- **`repeats`** — one field, three spellings, five recurring notes, scored on what a strict
  parser ADMITS. Answer: an iCalendar RRULE came back parseable **0 times in 113** values,
  and **0 in 115** on a sharpened spelling that names the format and forbids English; the
  note's own phrase parses 80 times in 118 but says what the note says only 28. The model
  writes `weekly` for "every Tuesday and Thursday" and `monthly` for "the first Monday of
  the month". **So the PRESENCE-not-MEMBERSHIP rule extends to grammars, not just word
  lists.** What does work: parsing the model's own verbatim `quote` recovers the rule on
  **198 of 200** runs, which is why `repeats` became a handler step rather than a field.
- **`ask`** and **`contradict`** — behavioural, and they run through `/api/debug/replay`
  instead of `/tool-probe`, because "does it ask" is never visible in a FIRST call. Both
  attach the real registry tools beside the candidate schema, drive the loop against canned
  tool results, and use the SHIPPED `note_ingest.prompt` (read from the file, not
  paraphrased — the persona's exact wording is the independent variable). No handler runs
  and nothing reaches the graph. `ask` found 1 silent guess in 106 runs on notes with an
  illegible value; `contradict` found the agent read the graph **0 times in 144** and called
  `ask_owner` **0 times**, including under a persona that told it to do both.

`SHAPE_PROBE_DUMP=<dir>` appends every raw run as JSONL so an arm can be re-scored offline.
Use it: the heuristics that classify a behavioural run (was that question about the
contradiction, is that value a guess or a hedge) are the weakest part of the probe, and a
count nobody can re-check is not a measurement.
