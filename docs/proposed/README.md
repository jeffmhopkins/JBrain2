# Proposed (not scheduled)

> **Status:** Living · **Last verified:** 2026-07-04

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
- `JPET_PLAN.md` — JPet, the wall pet: a Tron/synthwave **3D** wireframe robot that
  walks a room, an LLM companion for the kids (poke, tell, talk) with Sims-style drives
  (hunger/energy/mood) — **no training, no neural net**. Two surfaces over one
  server-authoritative `pet_state`: a **Wall** (3D WebGL/Three.js room) and a **phone
  Control screen** in the PWA, kept in sync by SSE fanout + `POST /pet/command`. Reuses
  the LLM adapter (new `pet.turn`/`pet.thought` tasks on the local model, a JPet settings
  card), scheduler ticks (drives off the single-threaded job queue → always second seat),
  the SSE transport, and the RLS domain firewall (scoped pet + kid principals — kids never
  see health/finance/location). Chosen mock: `../mocks/jpet/06-room-3d.html` (interactive
  3D). Waves: backend safety spine (W0) → realtime backbone (W1) → 3D Wall (W2) → phone
  Control (W3) → talk (W4) → memory/idle life (W5) → voice (W6).

_(The jcode plans, `GUIDED_INTAKE_PLAN.md`, and `SUBAGENT_SPAWNING_PLAN.md` were
promoted out of the icebox and have since shipped — see `../archive/`.)_
