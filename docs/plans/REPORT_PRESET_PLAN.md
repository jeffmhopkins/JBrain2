# Report Presets & Batch Runs — uniform reports, run down a list

> **Status:** In progress · **Last verified:** 2026-08-06 · **Waves:** P1✅ P2◻️

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
option. See the design artifact for the option analysis.
