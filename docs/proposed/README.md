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

- `PHOTO_ARCHIVE_PLAN.md` — photo archive pipeline: a staged, idempotent map over
  a decade of phone dumps (hash-keyed dedup, deterministic dating, a vision worker
  bridging pixels to the text-only 120B, CLIP search, InsightFace faces, residual
  RAG-backed date/identity inference, browser viewer).
