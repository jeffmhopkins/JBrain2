# Proposed (not scheduled)

> **Status:** Living · **Last verified:** 2026-07-18

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
_(The jcode plans, `GUIDED_INTAKE_PLAN.md`, and `SUBAGENT_SPAWNING_PLAN.md` were
promoted out of the icebox and have since shipped; `JPET_PLAN.md` and `JPET_V2_PLAN.md`
shipped and now live in `../archive/`. `EXTERNAL_VIDEO_INGESTION_PLAN.md`,
`DEEP_RESEARCH_TOOL_PLAN.md`, `DEEPEST_RESEARCH_TOOL_PLAN.md`, and
`VIDEO_IMAGE_TOOLS_PLAN.md` were promoted to `../plans/` and are in progress.)_
