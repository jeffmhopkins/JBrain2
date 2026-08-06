# Report Presets & Batch Runs — uniform reports, run down a list

> **Status:** In progress · **Last verified:** 2026-08-06 · **Waves:** P1✅ P2◻️ P3◻️

The deep-research engine plans each report's shape fresh every run, so two reports on
comparable subjects (say, two candidates on the same ballot) come out structurally
different. This plan adds two opt-in capabilities on top of the existing engine:

- **P1 — report presets** ✅: a saved, parameterized template that pins a report's section
  outline and its research angles, so the same subject-family comes out in one uniform
  shape. Design decision (owner, 2026-08-06): a **frozen parameterized plan** stored as a
  **checked-in file**, strictly **opt-in** — no preset falls back to today's self-orchestrating
  planner, byte-unchanged.
- **P2 — batch runs** ◻️: point a preset at a *list* of subjects and get one report each,
  run one-at-a-time in the background, notifying the owner per report and auto-advancing —
  "fire-through" (no blocking gate), on the existing deepest-research background lane.

Design options and prior art that led here: the studio design doc (chat artifact, 2026-08-06)
surveying GPT Researcher / STORM / open-deep-research / the commercial products, the
structured-generation literature (outline-first, form-filling, abstention sentinels), and the
repo seam map.

## P1 — Report presets ✅ (shipped)

**Shape.** A preset is the saved, variable-filled twin of the dict `deep_research._plan`
normally invents: `{sections, sub_questions}`. Supply it and the engine skips planning and
runs the fixed plan; omit it and the run self-orchestrates exactly as before.

**Files.**
- `backend/src/jbrain/agent/research_presets.py` — the loader + strict `{{ variable }}`
  renderer. Pure-YAML `*.preset` files, validated at import (a malformed preset fails
  startup); a render missing a variable, or naming an unknown preset, is a clean
  `PresetError` refusal, never a blank substitution. No `deep_research` import (one-way
  dependency), so `output_kind`/`source_mode` value checks live in the engine.
- `backend/src/jbrain/agent/presets/candidate_profile.preset` — the first preset: a uniform
  candidate profile (`{{candidate}}`, `{{office}}`). Its five gather angles bake in the
  accuracy checklist from the prior fix (search the FEC by name; read an authoritative bio,
  not just the campaign site; verify an absence before asserting it), and its objective
  carries the per-section fill spec + the `Not established — …` sentinel for an empty section.

**Engine seam** (`deep_research.py`). `_run` gained `plan_override` + `enforce_headings`.
The single branch is at the PLAN phase: with a `plan_override` the planner is skipped and the
fixed dict is used; otherwise `_plan` runs as before. `research()` gained an opt-in `preset`
+ `variables` path (`_run_preset`) that renders the preset, validates `output_kind`/`sources`,
caps the gather angles at `DR_MAX_BREADTH` (sections are uncapped — they aren't fans), and
drives `_run` with `enforce_headings=True`. The preset's objective rides the existing
deep_produce `objective` block, so no synth-prompt change was needed.

**Heading backstop.** `_backstop_critique` folds the existing zero-citation backstop together
with a new missing-heading check (preset runs only): if the writer dropped or renamed a
required section, one hardened re-synth (`STRUCTURE DEFECT`) forces it back — the same
one-retry, symptom-driven pattern as the citation backstop. Empty section → keep the heading
+ the honest sentinel, never a drop.

**Tool surface.** `deep_research.tool` gained `preset` + `variables` (and relaxed `required`
so a preset-only call — question derived from the preset — validates).

**Tests.** `test_research_presets.py` (loader/render/validation) and preset cases in
`test_deep_research.py` (planner skipped, fixed angles + outline used, missing-variable and
unknown-preset refusals, the heading backstop fires once on a preset run and never on a
default run, and the `_missing_headings` / `_backstop_critique` unit logic).

## P2 — Batch runs ◻️ (next)

Point a preset at a list; get one report each, serially, in the background, notifying per
report. Not the workflow engine — the batch rides the existing **deepest-research lane**
(`deepest_lane.py`), reusing `research_run_state` (a `batch-` run-id discriminates it) as the
durable list-cursor, `DeepResearchService.research(preset=…, variables=item, require_persist=True)`
per item, and the progress/notify channel per completion. Idempotency falls out of the report
library's `(question_hash, tool)` upsert. Fire-through by default; a verify-gate is a later
option. See the design artifact for the option analysis. (Note: the deepest-lane framing here
predates the owner's 2026-08-06 decision to avoid the deepest tool; the batch host is being
re-chosen — Jerv Tasks runner is the leading candidate — before P2 is built.)

## P3 — Compare-and-contrast preset (from the library) ◻️ (in progress)

A follow-up preset that produces ONE contrast-and-compare report across every candidate in a
race, synthesized **only from the per-candidate accountability profiles already in the research
library** — no web, no video corpus. Design decisions (owner, 2026-08-06):

- **Reports-only, no web (v1).** The compare draws exclusively from the owner's already-vetted,
  already-cited profiles. This is what makes "no fresh citations needed" coherent (every fact
  traces to a cited profile) AND makes the anti-hallucination check tractable (a closed, small
  source set that fits in context). A `reports_first` variant (reports as substance, web only to
  verify a specific time-sensitive fact, web-cited) is a later upgrade, not v1.
- **Dimension-parallel angles.** The 5 gather angles each compare ONE dimension (record, career,
  money+endorsements, positions/consistency, controversies) across the WHOLE field, with the
  candidate list passed as a `{{candidates}}` variable — so the fixed 5-angle preset handles a
  race of two candidates or nine, with no schema change.
- **Grounding gate (anti-hallucination).** Three layers: the writer may only restate what the
  profiles support and attributes every claim to a profile ("per X's profile"); the critique
  pass is run by a reports-reading reviewer that verifies the draft against the profiles and
  flags any unsupported claim; and (follow-on) a deterministic tripwire that flags any number/
  name/date in the draft absent from every source profile.

**Shipped so far (this branch):** `presets/compare_candidates.preset` (the outline, the
per-dimension angles, and the grounding/attribution objective). **Still to build:** a `reports`
source mode in `deep_research.py`; two sandboxed personas (`research_reports` gather +
`review_reports` grounding reviewer) with digest-pinned prompts and report-library-only tool
allowlists; the source-mode wiring (`_personas_for`, empty-gather message, etc.); the
deterministic tripwire; and tests. Sequenced AFTER `candidate_profile` merges and is tested —
the compare is only as good as the profiles feeding it.
