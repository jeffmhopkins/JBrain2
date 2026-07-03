# JBrain2 — Documentation map

> **Status:** Living · **Last verified:** 2026-07-03

JBrain2 is a personal knowledge system: notes in → RAG indexing → an
LLM-maintained wiki with notes as the sole sources of truth. This folder holds
the binding design docs. Project-wide non-negotiables live in the root
`CLAUDE.md`.

## Where the project is (2026-07)

**Phases 0–5 are shipped** — note capture,
ingestion/search, the v3 note→graph analysis pipeline, the personal agent
(tool-calling loop, Tier-A memory, Proposals/review inbox, external connectors,
the Full Brain chat surface), lists and appointments, and the **workflow engine**
(`events`/`triggers`/`pipelines`/`actions`/`runs` + scheduler + unified run-log +
the cutover of ingest/integration/consolidation onto the engine), reflexion in the
live turn, and the recurring self-heal reconcilers. The note-analysis calibration
evals run as a CI quality guard. The migration head advances continuously — see
`backend/migrations/versions/` for the current head.

**Phase 5 is complete; next is Phase 6 (Wiki).** The self-improvement Loops 2–4
(skill learning, durable-knowledge promotion, prompt/tool self-edit) and their
eval/promotion harness were **removed** — only Loop 1 (reflexion) shipped. The
not-yet-built hygiene sweeps are deferred to Phase 6. See `ROADMAP.md`; the completed
Phase-5 build record is `archive/PHASE5_COMPLETION_PLAN.md`.

## Documentation map

Docs are organized **by kind** — each folder owns its own index, so this map
stays thin and doesn't drift against the folders it points at.

| Folder | Kind | Index |
|---|---|---|
| `reference/` | How the system **is** — architecture, standards, and models (Living, binding). | `reference/README.md` |
| `runbooks/` | How to **operate** the box — setup, access, recovery. | `runbooks/README.md` |
| `plans/` | **Active** multi-wave build plans (`Scheduled` / `In progress` / `Parked`). | `plans/README.md` |
| `proposed/` | **Icebox** — forward-looking specs, not on the roadmap. | `proposed/README.md` |
| `archive/` | **History** — completed plans, a fulfilled contract, a rejected design, research. | `archive/README.md` |
| `mocks/` | Binding **HTML UI spec** (per `reference/DESIGN.md`). | — |

**Start here:** `ROADMAP.md` (what's next) · `reference/ARCHITECTURE.md` (the
system shape) · `reference/DEVELOPMENT.md` (binding standards) ·
`DOC_LIFECYCLE.md` (how these docs are born, kept true, and archived).

At the top level, beside this map: `ROADMAP.md` (phase plan + status) and
`DOC_LIFECYCLE.md` (the doc process, enforced by the `docs` CI gate
`scripts/docs-freshness.sh`). The one-time cleanup that adopted the lifecycle and
sorted docs into these folders is `archive/DOC_CLEANUP_PLAN.md`.
