# Proposed (not scheduled)

> **Status:** Living · **Last verified:** 2026-08-17

Forward-looking design specs **dropped in for the record but not on the
roadmap** — the icebox: ideas worth keeping shaped, kept out of the active-plan
list in `../README.md` so they're never mistaken for in-flight work. Per
`../DOC_LIFECYCLE.md`, this folder holds `Proposed` docs only — nothing built,
nothing rejected. A built design moves to `../archive/`; a killed design moves
to `../archive/` with a `Rejected` banner.

When a doc here is picked up, it must be reconciled with the root `CLAUDE.md`
non-negotiables (LLM adapter, storage abstraction, RLS + isolation tests, etc.),
given a roadmap slot in `../ROADMAP.md`, and promoted out of this folder.

## Contents

- `JERV_CONTEXT_BUDGET_PLAN.md` — rebalance what a jerv turn spends its context on
  (44 tools ≈ 28.7k tokens of schema per turn, nothing carried across sessions). W1 fixes the
  identity paragraph that made the on-box model discount its own prompt; W2 adds a
  cross-session **scratchpad** on a generalized `app.agent_scratchpad` table (the archivist's
  memory migrates into it) split by **write authority** — the agent writes only dated
  `Threads`/`Watch`, injected inside the `briefs.py` untrusted-data sentinel, while
  `Preferences`/`Corrections` are owner-written from the PWA and carry sanctioned-instruction
  framing; W3 puts config-derived lists into the tool schemas at `schemas_for` time. The
  description trim lives in `TOOL_CATALOG_PLAN` W0b, which W2 must land after. **Revised after
  a four-lens adversarial review** (§8); records five deliberate rejections with corrected
  evidence (Python sandbox, skills-in-memory, per-persona sidecars, `anyOf`, catalog W2/W3).
- `PHOTO_ARCHIVE_PLAN.md` — photo archive pipeline: a staged, idempotent map over
  a decade of phone dumps (hash-keyed dedup, deterministic dating, a vision worker
  bridging pixels to the text-only 120B, CLIP search, InsightFace faces, residual
  RAG-backed date/identity inference, browser viewer).
- `MUSIC_GEN_PLAN.md` — music generation on the existing opt-in `comfyui` service
  (ACE-Step 1.5 XL Turbo, AMD/gfx1151-validated): a new audio workflow + audio-aware
  driver output path, an owner-only `generated_audio` artifact table, a `generate_music`
  tool, and a MusicScreen — mirroring the shipped image stack. Backend (Waves M0–M3) +
  frontend (M4), with M0 a blocking on-box host-validation spike. Interactive mock:
  `../mocks/music-gen-live/live-music-tool-card.html`.
- `TEACHER_MODE_AGENTS_PLAN.md` — split the `teacher` persona into two agents:
  an owner **instructor** (authors/approves lessons + curricula, assigns to a
  child, reviews results) and a sandboxed non-owner **student** behind an
  anonymous scoped link (a live, KB-less tutor). Clones the shipped intake-link
  substrate; net-new lesson/curriculum domain model + server-owned lesson-runtime
  state machine + two-sided child-safety layer + two UIs. Waves W1–W8 with a hard
  safety gate before child exposure. Backed by the approved component work in
  `../research/teacher-mode/` (`COMPONENT_CATALOG.md` + four mocks).
- `TOOL_CATALOG_PLAN.md` — a scalable tool surface for jerv's growing tool count:
  separate DISCOVERY (an always-on compact menu — name + ≤12-word summary + family)
  from INVOCATION-SCHEMA (a verbose use-guide loaded on demand via `tool_guide(name)`,
  which also arms the tool's schema), plus a full-schema hot core for the common path
  and umbrella dispatch tools for the source/action families. Revised after two
  independent reviews: ship the cheap waves now (W0 trim + metadata, W1 umbrellas
  19→4), and **gate** the catalog machinery (W2/W3) behind resolving the
  mode-(a)/native-tool-calling contradiction and a pre-built selection-accuracy eval.
- `DEEP_RESEARCH_MODULE_SPLIT_PLAN.md` — break the ~2,300-line `deep_research.py`
  orchestrator monolith into topic modules (`research_sources.py`, `research_directives.py`,
  `research_report_view.py`, `research_backstops.py`), leaving `DeepResearchService` in
  `deep_research.py`. A pure mechanical move with NO behaviour change; the one gotcha is
  re-exporting the moved private helpers from `deep_research` so the tests' imports still
  resolve. Its own PR, done in a git-push-capable session. Follows from the scratchpad work
  (PR #1049).
_(The jcode plans, `GUIDED_INTAKE_PLAN.md`, and `SUBAGENT_SPAWNING_PLAN.md` were
promoted out of the icebox and have since shipped; `JPET_PLAN.md` and `JPET_V2_PLAN.md`
shipped and now live in `../archive/`. `EXTERNAL_VIDEO_INGESTION_PLAN.md`,
`DEEP_RESEARCH_TOOL_PLAN.md`, `DEEPEST_RESEARCH_TOOL_PLAN.md`, and
`VIDEO_IMAGE_TOOLS_PLAN.md` were promoted to `../plans/` and are in progress.
`ENTITY_GRAPH_INGEST_V2_PLAN.md` was ratified and promoted to `../plans/` (Scheduled).
`DEEP_PRODUCE_PLAN.md` was reviewed, hardened, and promoted to `../plans/` (Scheduled).
`CROSS_TURN_TOOL_RESULTS_PLAN.md` was promoted to `../plans/` (In progress — W0+W1 landed).
`DYNAMIC_PORTAL_FETCH_PLAN.md` was scheduled and promoted to `../plans/` (In progress — P3 landed).
`GROKIPEDIA_TOOL_PLAN.md` was promoted, shipped (W1–W3, PR #993), and now lives in `../archive/`.)_
- **`LOCAL_ONLY_BOX_PLAN.md`** — co-residency without surprises. Keeps `gpt-oss-120b` and the 27b
  co-resident by closing the paths that load or evict silently: a `stopping` model that reads as
  resident and gets relaunched with no admission, a gateway config regen that shuts down every
  running server invisibly, two unadmitted debug-console loads, and refusals that emit no event at
  all. Then an owner park control, PWA-recoverable local hosting, and cloud retirement — which
  needs no code to begin, since a keyless provider is already hidden and any task can be re-pointed
  from the PWA today. Successor to the superseded `../archive/GPU_ADMISSION_INTEGRITY_PLAN.md`.
