# Deep Research — staged single-source pipeline (the interview fix)

> **Status:** In progress · **Last verified:** 2026-07-30 · **Waves:** W1✅ W2◻️ W3◻️

Teach the shipped `deep_research` / `deep_produce` engine to run a **staged,
dependency-aware** pipeline so it can process a *single known source* — the motivating
case being *"extract every question from this interview, answer each, fact-check each
against the web, and tabulate."* Today the engine flattens that sequential job into a
**parallel fan of independent angles**, which is the wrong shape and fails three ways at
once. W1 first lands the **observability** (child sub-agent tool-call persistence) that
this debugging needed and that will verify W2's fix; W2 adds the staged/feed-forward
runner; W3 adds the single-source primitives (whole-transcript read, enumeration mode,
`table` output, jerv routing).

## The motivating failure (2026-07-30, live box)

A run of *"From the video `XuoqKYxDHVc` extract the main questions, give concise answers,
and for each answer provide a brief analysis that fact-checks against up-to-date web
sources. Present as a table: Question, Answer, Analysis"* — `sources=library_first`,
`mode=standard` — over the 85-minute *Economist* Elon Musk interview. The planner
decomposed it into **three gather angles spawned as parallel siblings at the same
instant** (all `research_library`, corpus-only, no web):

| child | persona | web? | outcome |
|---|---|---|---|
| Extract Video Questions | research_library | ✗ | 14 questions, clustered 0:00–13:46 + two stragglers at 37:00/41:30; **nothing past 41:30** of 85 min |
| Provide Concise Answers | research_library | ✗ | re-derived its **own, divergent** question list (no overlap with the extractor's) |
| Fact-Check and Analyze | research_library | ✗ | *"I'm unable to verify against external web sources, as I can only access the video library."* |

Three root causes, each traced to code:

1. **No coordination → duplicated, divergent work.** `run_research_fan`
   (`spawn.py:566`) runs all briefs concurrently with **no data flowing sibling→sibling**;
   `deep_research_plan.prompt:36-38` even *mandates* independence ("no sub-question may
   depend on… another sub-question's answer"). So "Provide Answers" ran simultaneously
   with "Extract Questions" and guessed its own questions — the two lists don't overlap.
2. **Fact-check had no web.** Under `library_first`, `_personas_for` (`deep_research.py:152`)
   runs the **whole gather fan** as `research_library` (no web tools); web only exists in
   the reflect→refill round. A gather angle named "fact-check against the web" held no web
   tool.
3. **Extraction incomplete.** The transcript is fully indexed (70 chunks, 80,839 chars, to
   84.9 min), so nothing is missing upstream. But: `read_external_video` **truncates at
   `_TRANSCRIPT_MAX_CHARS = 60_000`** (`externaltools.py:58,180`) — the last ~26% (~22 min)
   never enters context — and `research_library.prompt:46-50` tells the child to stop early
   ("a handful of targeted searches… STOP as soon as you have enough"), correct for "find
   an answer" and wrong for "enumerate all X". The extractor stopped at 41:30, *before* even
   the truncation point.

Child tool-call traces are **not persisted** (`_persist_child` writes `tools=[]`,
`spawn.py:1195`), so none of this was visible from storage — only the brief-in/summary-out
boundary is. That is why W1 (observability) comes first.

## The core insight

`deep_research` is built for a **research question**: decompose into *independent parallel
angles* → each searches → synthesize *prose*. The interview task is a different shape:

| the engine assumes | the interview task needs |
|---|---|
| a question to investigate | a **single known source** to process |
| independent parallel angles | **sequential stages** (extract → answer → fact-check) |
| representative findings | **exhaustive enumeration** (every item) |
| a synthesized prose report | a **table keyed by item** |
| one source family per run | library for *extraction*, web for *fact-check*, in one run |

Every failure follows from the mismatch, so the fix is to let the engine express and run
*dependent stages*, each choosing its own persona/tools — not three separate patches.

## Reuse vs. net-new (the lean litmus, `ASSISTANT.md`)

- **Reuse:** the feeding-waves envelope (`compose_feed_block`/`prepend_feed`,
  `briefs.py`) already carries one agent's output to another as fenced inert data — used
  today gather→analyst; W2 extends it to gather→gather. `_personas_for` /
  `effective_child_tools` already pick and clamp a fan's tools per source mode. `library_first`
  already models "extract from library, fill from web". `output_kind=table` +
  `_shape_directive` already exist (`deep_research.py:1355`). The whole
  plan→analyze→critique spine, the `deep_research_report` view, and the `_run` engine are
  unchanged.
- **Net-new (small):** a `stages`/`depends_on` field in the plan schema; a sequential
  runner that feeds each stage forward and picks its persona; a windowed/uncapped
  single-video read; an enumeration-mode prompt clause; and the child tool-step
  accumulation W1 adds to the loop.

## W1 — child sub-agent tool-call + reasoning persistence (observability)

**Problem.** A child runs the *non-streaming* `AgentLoop.run` (`loop.py:444`) that returns
only `AgentResult(text, web_sources)`. Its tool steps are surfaced **live** as
`SubagentToolEvent` (`spawn.py:1031`, via the `_on_tool` callback) and its reasoning as
`SubagentDeltaEvent` — that's the app's live "N tools" / Thinking trace — but both are
dropped server-side. `_persist_child` writes `tools=[]` and no reasoning (`spawn.py:1195`),
so a reopened sub-agent session (and any later debug SQL) shows only brief→answer.

**Fix.** Capture the child's steps during `run` and persist them to its own
`agent_turns` row in the **same shape the parent uses**, so replay and debugging work
identically. The parent's shape is defined once in `TranscriptAccumulator`
(`transcript_accumulator.py:39-88`): per step `{id, name, ok, args, summary, sources,
web_sources, proposal, entities, view, text_offset, reasoning_offset}`.

- **`loop.py`:** in `run`, accumulate a `tool_steps: list[dict]` as each `dispatched`
  tool call settles (all fields are already in hand at `loop.py:580`: `dispatched.result`,
  `.sources`, `.web_sources`, `.proposal`, `.entities`, `.view`), and accumulate the
  reasoning text (wrap the `on_reasoning` callback). Return both on `AgentResult` via
  **new optional fields** (`tool_steps: tuple[dict, ...] = ()`, `reasoning: str = ""`) —
  additive, so every existing caller is unaffected.
- **Result-size cap (matters more for children).** A child reads big transcripts
  (`read_external_video` → up to 60k chars). Cap each persisted step `summary` to a bounded
  size (`_CHILD_STEP_SUMMARY_MAX`, a few KB) with a truncation marker, so a run's JSONB
  stays bounded. The parent doesn't need this (owner turns rarely dump a 60k tool result);
  children do.
- **`spawn.py`:** `_run_child` passes `result.tool_steps` / `result.reasoning` to
  `_persist_child`, which threads them into `record_exchange(tools=…, reasoning=…)` instead
  of `tools=[]`.
- **Frontend:** a reopened sub-agent session already renders `tools` as the "Worked" list
  (the live sub-agent frame renders the same `SubagentToolEvent`s; `transcript.ts`
  `ToolActivity` carries **optional** `textOffset?`/`reasoningOffset?`, `:110,114`). Verify
  the session-load path maps a persisted child `tools` dict → `ToolActivity`; add the
  mapping only if it's missing. **No new markup or view** — this is replay of an existing
  render pattern.

**No migration** — `agent_turns.tools` (JSONB) + `.reasoning` already exist and children
already write the row; W1 only populates fields currently left empty.

**GUI note.** Making a reopened sub-agent session replay its tool steps is a **trivial
reuse** of the shipped sub-agent "Worked" rendering (the same steps already render live),
not a new/changed surface — no `PROCESS.md` mock gate. Flagged for owner judgement at
build time, mirroring the DV2 chip call in `DEEP_RESEARCH_VIDEO_SOURCES_PLAN.md`.

## W2 — staged gather with feed-forward + per-stage persona (coordination + fact-check)

The structural fix for causes #1 and #2 together — both reduce to *"the plan has ordered
stages, and each stage picks its own persona/tools and consumes the prior stage's output."*

- **Plan schema (additive).** Extend `_PLAN_SCHEMA` (`deep_research.py:250`) so a plan may
  emit **ordered stages** instead of (or alongside) a flat `sub_questions` list — e.g. a
  `stages: [{title, brief, persona_hint, feeds_next: bool}]` array. **A single-stage plan
  is byte-identical to today's flat gather** (one parallel fan), so the default research
  path cannot regress — new behaviour lights up only when the planner emits ≥2 dependent
  stages. `deep_research_plan.prompt` gains a clause: when the objective is a
  *single-source extract→transform→enrich* task, emit dependent stages; otherwise keep the
  independent-angles rule verbatim (bump `dr-plan-v5 → v6`, digest-pinned).
- **Sequential staged runner.** A new path in `_run` that, for a multi-stage plan, runs
  stage 1, then feeds its findings forward (via `prepend_feed`/`compose_feed_block` — the
  existing inert-data envelope) into stage 2's briefs, and so on. Stage N's fan is a normal
  `run_research_fan`, so every child guard (parent⊆child clamp, `no_memory` sandbox, SSRF
  egress, tree budget/wall-clock, reserves) is unchanged. Reuses the analyst/critique
  reserves already carved off (`deep_research.py:663`).
- **Per-stage persona.** Each stage picks its persona from the source mode **and its role**:
  under `library_first`, an *extract* stage runs `research_library` (corpus) and an
  *answer/fact-check* stage runs the web `research` persona — generalizing the existing
  "gather=library, refill=web" split onto the stages. So fact-checking against the web
  becomes a first-class stage with the right tools, not a corpus agent asked to do what it
  can't. Reuses `effective_child_tools` for the clamp.
- **Convergence + bounds.** A hard cap on stages (mirror `DR_MAX_GAP_QUESTIONS`/round
  caps); a stage that produces nothing usable stops the chain (like the refill's
  `coverage_limited`); the whole run stays in-request, depth-1, under the tree deadline.

**Security.** No new egress or scope surface. Feed-forward reuses the fenced
`<untrusted_external_data>` envelope, so a transcript that says "ignore your brief" cannot
steer a downstream stage (the boundary the corpus tools + `research_library.prompt` already
enforce). Per-stage web egress under `library_first` is the *same* SSRF-guarded
`web_search`/`web_fetch` the refill round already uses; `library` (exclusive) mode keeps
**zero web on every stage** (assert it, as the shipped exclusive-guarantee test does).

## W3 — single-source primitives + table output + routing (completeness + shape)

- **Whole-transcript read.** For a deliberate single-video read, the 60k cap and the
  "stop early" prompt both hide the back of the source. Add a **windowed read** —
  `read_external_video(url, from_ms=…)` (or a `part`/`page` param) — so an agent can sweep
  an 80k-char transcript across two calls, and/or raise the cap for a single explicit read.
  The cap exists to stop an *unbounded corpus dump* from swamping jerv; a deliberate
  single-source read is exactly the case it over-restricts. (`.tool` version bump on
  `read_external_video`.)
- **Enumeration mode.** An `research_library`/`research` brief that is an *enumeration*
  ("list ALL X") needs a prompt clause that overrides "stop as soon as you have enough" —
  read the whole source in order and enumerate exhaustively. Add it as a brief-carried
  directive (no persona fork) or a small prompt clause, digest-pinned.
- **Table output keyed by item.** Route the interview task through
  `deep_produce(output_kind="table")` so the writer emits the Question/Answer/Analysis
  table the owner asked for (one row per extracted item), not a prose report — reusing the
  shipped `_shape_directive`/`table` and the `deep_research_report` view (the artifact is a
  `.md` document, no new GUI surface, per `DEEP_PRODUCE_PLAN.md`).
- **Routing.** `jerv.prompt` gains steering: a *single-source structured-extraction*
  request ("extract the questions from this video and tabulate", "pull the action items
  from this transcript") reaches for `deep_produce(output_kind=table, sources=library_first)`
  with the staged plan, rather than `deep_research(report)`. Version bump + digest pin.

## Waves

Each wave: local `ruff`+`pyright`+unit tests before it lands; an **independent adversarial
review** (reviewer ≠ builder) per `PROCESS.md`; the security/boundary surface (W2's
feed-forward + per-stage egress) gets a red-team pass.

| Wave | Scope | Gate |
|---|---|---|
| **W1 — child tool-call + reasoning persistence ✅ (landed on-branch)** | `loop.py` accumulates `tool_steps`+reasoning on `AgentResult` (additive fields, per-step summary capped at 4 KB — the model still sees the full result in-context); `spawn.py` `_persist_child` threads them into `record_exchange`; frontend needs **no change** — `fromTurn` (`useFullBrain.ts:168`) already hydrates any session's stored `tools`/`reasoning`. `text_offset` is 0 for a child step (the persisted content is the final answer, which every call precedes); `reasoning_offset` indexes the full reasoning trace. No migration. Independently reviewed (reviewer ≠ builder); one low-severity `text_offset` fidelity finding fixed. | A child with N tool steps persists N `tools` entries + reasoning (was `[]`); `load` round-trip; step-summary cap holds; the forced-final path carries the trace; existing `deep_research`/spawn suites green (134 unit). |
| **W2 — staged gather + feed-forward + per-stage persona** | additive `stages` plan schema (single-stage ≡ today, byte-stable); sequential staged runner feeding each stage forward via the inert-data envelope; per-stage persona from source mode + role (extract=library, answer/fact-check=web under `library_first`); stage/convergence bounds; `dr-plan-v6` prompt. | single-stage plan runs byte-identically to today's flat gather; a 2-stage plan feeds stage 1 → stage 2 (no re-derivation); `library_first` extract stage holds no web, answer stage holds web; `library` mode issues zero `web_*` on every stage (exclusive guarantee); injection: a poisoned stage-1 finding cannot steer stage 2; tree bounds honored. |
| **W3 — single-source primitives + table + routing** | windowed/uncapped single-video read (`read_external_video` `.tool` bump); enumeration-mode clause; `deep_produce(output_kind=table)` for the interview shape; `jerv.prompt` routing bump. | a windowed read returns the whole 80k transcript across calls; an enumeration brief sweeps the full source; a table run emits the `deep_research_report` view with a Markdown table keyed per item; jerv routes a single-source extract request to the staged `table` path; `.prompt`/`.tool` digest pins. |

W2 depends on W1 only for *verification* (W1 is the instrument proving W2 removes the
duplicate searches), not compilation — they can build independently, but land W1 first.
W3 depends on W2's staged runner.

## Testing (per `CLAUDE.md` #5 — 80% backend, security 100%, real Postgres, LLM faked)

- **W1:** unit — a faked multi-tool child returns `tool_steps` in the parent shape with a
  capped summary + accumulated reasoning; `_persist_child` persists them (extends
  `test_child_brief_and_answer_persisted_to_its_own_transcript`, which today asserts the
  boundary only); a real-Postgres `record_exchange`→`load` round-trip reads the child's
  tools/reasoning back **owner-scoped** (RLS); frontend replay renders a persisted child
  Worked list.
- **W2:** routing — a single-stage plan is byte-identical to today's flat gather
  (characterization); a multi-stage plan runs stages in order and feeds forward (assert
  stage 2's brief contains stage 1's fenced findings, and no second question-derivation);
  per-stage persona per source mode; the **exclusive guarantee** (`library` → zero `web_*`
  on every stage, 100% security path); the **injection boundary** (a poisoned stage-1
  finding fed as escaped data cannot trigger a stage-2 tool call or steer it).
- **W3:** a windowed `read_external_video` returns disjoint transcript spans covering the
  whole source; an enumeration brief is exhaustive over a seeded multi-window fixture; a
  `table` run persists + renders a per-item table; digest pins for every bumped
  `.prompt`/`.tool`.

## Open decisions

1. **Stage schema shape.** A dedicated `stages` array vs. a `depends_on` edge on the
   existing `sub_questions`. Recommend a `stages` array — the planner reasons about "waves"
   more reliably than a dependency graph on a local model, and a single-stage array trivially
   reduces to today's flat fan.
2. **Windowed read vs. cap lift.** A `from_ms` window (bounded per call, composable) vs.
   simply raising `_TRANSCRIPT_MAX_CHARS` for a single explicit read. Recommend the window —
   it keeps any one tool result bounded (context-safe) while allowing full coverage; a naive
   cap lift risks a single 200k-char corpus dump on a long source.
3. **Enumeration signal.** Detected from the brief text (a heuristic) vs. an explicit plan
   flag (`exhaustive: true` on a stage). Recommend the explicit plan flag — the planner
   already knows "extract ALL questions" is exhaustive; a flag is testable, a heuristic isn't.
4. **How much to auto-route vs. leave to jerv.** W3 can steer jerv to the staged `table`
   path, or the engine can detect a single named source in the objective and stage itself.
   Recommend jerv steering first (no engine magic), engine auto-detection as a later option.

## Reconciliation on promotion (per `DOC_LIFECYCLE.md`)

Filed `Scheduled` in `plans/` with a `ROADMAP.md` slot + `plans/README.md` row (this PR).
On each wave merge: flip to `In progress`, tick the wave marker (header + body), bump
`Last verified`. On the last wave: flip to `Shipped`, `git mv` to `archive/`, carry any
residual into `ROADMAP.md`, update `archive/README.md` + `plans/README.md`. Reconcile the
Living behaviour docs each wave touches — `ASSISTANT.md` (sub-agent replay now carries tool
steps; the staged pipeline), `DEEP_RESEARCH_VIDEO_SOURCES_PLAN.md` (per-stage persona
generalizes `library_first`), and `EXTERNAL_VIDEO_INGESTION_PLAN.md` (windowed read) — in
the PRs that change those behaviours.
