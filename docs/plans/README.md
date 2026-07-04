# Plans — active build plans

> **Status:** Living · **Last verified:** 2026-07-03

Active, multi-wave build plans (`Scheduled` / `In progress` / `Parked`, per
`../DOC_LIFECYCLE.md`). A plan archives to `../archive/` in the PR that lands its
last wave; proposed-but-unscheduled ideas live in `../proposed/`.

| Doc | Status |
|---|---|
| `JPET_PLAN.md` | **In progress** — JPet, the family wall pet (Phase 7): a 3D Tron/synthwave wireframe robot that walks a room; an LLM companion the kids drive from a **phone Control screen** and watch on a **Wall** (Three.js), synced over one server-authoritative `pet_state` via SSE + `POST /pet/command`. Local-model brain, drives off the job queue, scoped pet+kid principal firewall. Chosen mocks: `../mocks/jpet/06-room-3d.html` + `07-phone-control.html`. W0–W4 landed (safety spine `pet_state`+RLS+tick/migration 0123; `/api/pet` GET/command/stream SSE fan-out; 3D WebGL `WallScreen`; mobile `ControlScreen`; `pet.turn` talk brain — `say` + talk box + Wall speech bubble); W5 (memory + autonomous life) next. |
| `EMR_IMPORT_PLAN.md` | **In progress** — EMR / medical-record import: multi-system EMR PDF exports normalized into cited, health-firewalled `measurement` + `event` facts, with `lab_results`/`encounters` projections and `read_labs`/`read_encounters` tools. W0 (gates + synthetic fixtures) done; W1–W5 open. |
| `PHASE6_WIKI_PLAN.md` | **In progress** — Phase 6 (Wiki). Waves A–C shipped (builder, citations, Talk); Wave D open (re-enable schedules, grounding-gate tuning, purge→rebuild). |
| `JCODE_SESSION_ISOLATION_PLAN.md` | **Parked** — per-session network namespace; the P0 substrate was reverted after the P1 spike. Kept for a future revisit. |
