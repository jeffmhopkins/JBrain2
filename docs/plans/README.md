# Plans — active build plans

> **Status:** Living · **Last verified:** 2026-07-06

Active, multi-wave build plans (`Scheduled` / `In progress` / `Parked`, per
`../DOC_LIFECYCLE.md`). A plan archives to `../archive/` in the PR that lands its
last wave; proposed-but-unscheduled ideas live in `../proposed/`.

| Doc | Status |
|---|---|
| `EMR_IMPORT_PLAN.md` | **In progress** — EMR / medical-record import: multi-system EMR PDF exports normalized into cited, health-firewalled `measurement` + `event` facts, with `lab_results`/`encounters` projections and `read_labs`/`read_encounters` tools. W0 (gates + synthetic fixtures) done; W1–W5 open. |
| `PHASE6_WIKI_PLAN.md` | **In progress** — Phase 6 (Wiki). Waves A–C shipped (builder, citations, Talk); Wave D open (re-enable schedules, grounding-gate tuning, purge→rebuild). |
| `JCODE_SESSION_ISOLATION_PLAN.md` | **Parked** — per-session network namespace; the P0 substrate was reverted after the P1 spike. Kept for a future revisit. |
| `READ_ALOUD_LEGIBILITY.md` | **Scheduled** — move piper out of the wall and colocate it with whisper as a default `tts-stt` speech service (rename `server-brain`→`wall`) with a warm model cache; normalize answer text to speakable prose (markdown/emoji/tables/pauses); make streaming an adaptive pipelined chunker. W0 (split/colocate) → W1 (legibility) → W2 (fluid chunking). |
