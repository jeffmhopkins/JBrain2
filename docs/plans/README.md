# Plans — active build plans

> **Status:** Living · **Last verified:** 2026-07-06

Active, multi-wave build plans (`Scheduled` / `In progress` / `Parked`, per
`../DOC_LIFECYCLE.md`). A plan archives to `../archive/` in the PR that lands its
last wave; proposed-but-unscheduled ideas live in `../proposed/`.

| Doc | Status |
|---|---|
| `EMR_IMPORT_PLAN.md` | **In progress** — EMR / medical-record import: multi-system EMR PDF exports normalized into cited, health-firewalled `measurement` + `event` facts, with `lab_results`/`encounters` projections and `read_labs`/`read_encounters` tools. W0 (gates + synthetic fixtures) done; W1–W5 open. |
| `PHASE6_WIKI_PLAN.md` | **In progress** — Phase 6 (Wiki). Waves A–C shipped (builder, citations, Talk); Wave D open (re-enable schedules, grounding-gate tuning, purge→rebuild). |
| `READ_ALOUD_AUDIOBOOK_PLAN.md` | **In progress** — audiobook-grade read-aloud on the on-box Kokoro engine: a two-layer text pipeline, misaki G2P + lexicon pronunciation, shaped silence/pacing, a blended narrator voice, and story-vs-answer modes. Single narrator; builds on `../archive/READ_ALOUD_LEGIBILITY.md`. W0 (split) + W1 (misaki) + W2 (pacing) + W3 (narrator blends) landed; W4 (modes + prosody, GUI-gated) open. |
| `JCODE_SESSION_ISOLATION_PLAN.md` | **Parked** — per-session network namespace; the P0 substrate was reverted after the P1 spike. Kept for a future revisit. |
