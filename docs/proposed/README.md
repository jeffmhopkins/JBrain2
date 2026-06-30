# Proposed (not scheduled)

Forward-looking design specs that are **dropped in for the record but not on the
roadmap** — nothing here is built and none of it is committed to a phase. This is
the icebox: ideas worth keeping shaped, kept out of the active-plan list in
`../README.md` so they're never mistaken for in-flight work.

Distinct from:

- **`../*_PLAN.md`** — build plans for shipped or in-flight work (e.g.
  `VIDEO_ANALYSIS_PLAN.md`, `IMAGE_GEN_PLAN.md`), tracked in `../README.md`.
- **`../archive/`** — completed build plans and the research that fed them.

When a doc here is picked up, it must be reconciled with the root `CLAUDE.md`
non-negotiables (LLM adapter, storage abstraction, RLS + isolation tests, etc.),
given a roadmap slot in `../ROADMAP.md`, and promoted out of this folder.

## Contents

- `JCODE_PLAN.md` — **jcode** code mode: an opt-in, on-box **sidecar** running Claude
  Code's agent engine against a local coder model (Qwen3-Coder-Next 80B-A3B), fronted by
  the PWA (launcher → session). Local-only (no cloud/terms surface), reads no knowledge
  base, works in isolated per-session git checkouts, with an optional per-session preview
  tunnel. Five-wave build plan; one owner-only `jcode_sessions` table. Reference mocks:
  `../mocks/jcode-launcher.html`, `../mocks/jcode-session.html`.
- `PHOTO_ARCHIVE_PLAN.md` — photo archive pipeline: a staged, idempotent map over
  a decade of phone dumps (hash-keyed dedup, deterministic dating, a vision worker
  bridging pixels to the text-only 120B, CLIP search, InsightFace faces, residual
  RAG-backed date/identity inference, browser viewer).
- `GUIDED_INTAKE_PLAN.md` — **guided-intake share links** (realizes the Phase 7 roadmap line):
  the owner mints a time-boxed, run-capped link that gives a recipient a chat interface to a
  scoped AI interviewer; the interviewer drafts a summary, the recipient confirms it, and the
  captured submission surfaces to the owner as an editable/approvable Proposal that becomes
  attributed notes. Reuses the jcode share-link auth, the Proposal primitive, the agent loop,
  and notes→facts ingestion; one closed `intake` persona, two owner-RLS tables, an editable
  intake-link Proposal, and a read-only conversation view kept separate from the owner's chats.
  Chosen mocks in `../mocks/guided-intake/` (recipient, Proposal editor, management screen).
- `MUSIC_GEN_PLAN.md` — music generation on the existing opt-in `comfyui` service
  (ACE-Step 1.5 XL Turbo, AMD/gfx1151-validated): a new audio workflow + audio-aware
  driver output path, an owner-only `generated_audio` artifact table, a `generate_music`
  tool, and a MusicScreen — mirroring the shipped image stack. Backend (Waves M0–M3) +
  frontend (M4), with M0 a blocking on-box host-validation spike. Interactive mock:
  `../mocks/music-gen-live/live-music-tool-card.html`.

_(`SUBAGENT_SPAWNING_PLAN.md` was promoted out of the icebox — it is now the
scheduled build plan `../SUBAGENT_SPAWNING_PLAN.md`, listed under "Active plan" in
`../README.md`.)_
