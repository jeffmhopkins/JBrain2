# Plans — active build plans

> **Status:** Living · **Last verified:** 2026-07-03

Active, multi-wave build plans (`Scheduled` / `In progress` / `Parked`, per
`../DOC_LIFECYCLE.md`). A plan archives to `../archive/` in the PR that lands its
last wave; proposed-but-unscheduled ideas live in `../proposed/`.

| Doc | Status |
|---|---|
| `PHASE6_WIKI_PLAN.md` | **In progress** — Phase 6 (Wiki). Waves A–C shipped (builder, citations, Talk); Wave D open (re-enable schedules, grounding-gate tuning, purge→rebuild). |
| `WIKI_LINT_PLAN.md` | **Scheduled** — `wiki_lint`, the corpus-wide wiki health sweep (fifth in-code ActionSpec). Wave 0 (owner ratification), Wave A (deterministic no-LLM checks → re-dirty + Talk + runs), Wave B (LLM contradiction/stale-claim review cards). |
| `JCODE_SESSION_ISOLATION_PLAN.md` | **Parked** — per-session network namespace; the P0 substrate was reverted after the P1 spike. Kept for a future revisit. |
