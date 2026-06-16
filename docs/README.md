# JBrain2 — Documentation map

JBrain2 is a personal knowledge system: notes in → RAG indexing → an
LLM-maintained wiki with notes as the sole sources of truth. This folder holds
the binding design docs. Project-wide non-negotiables live in the root
`CLAUDE.md`.

## Where the project is (2026-06)

**Phases 0–4 and the Phase 5 workflow engine are shipped** — note capture,
ingestion/search, the v3 note→graph analysis pipeline, the personal agent
(tool-calling loop, Tier-A memory, Proposals/review inbox, external connectors,
the Full Brain chat surface), lists and appointments, and the **workflow engine**
(`events`/`triggers`/`pipelines`/`actions`/`runs` + scheduler + unified run-log +
the cutover of ingest/integration/consolidation onto the engine), reflexion in the
live turn, a fed eval harness, and the recurring self-heal reconcilers. Migrations
run through 0043.

**Next: Phase 5 residual completion → Phase 6 (Wiki).** Phase 5's engine is live;
what remains (self-improvement Loops 2–4 and the not-yet-built hygiene sweeps) is
deferred to Phase 6. See `PHASE5_COMPLETION_PLAN.md` and `ROADMAP.md`.

## Living reference (read these)

| Doc | What it covers |
|---|---|
| `ARCHITECTURE.md` | System shape: containers, the one-database design, the knowledge pipeline, security model, operations. |
| `ROADMAP.md` | Phase plan and current status. The source of truth for "what's next." |
| `DEVELOPMENT.md` | Binding standards: the architectural constitution, comments, testing, git, releases, `dev-setup.sh`. |
| `PROCESS.md` | Binding multi-wave execution process for plan work: parallel tasks, per-task + per-wave adversarial review, one PR per wave, the GUI mock gate. |
| `DESIGN.md` | Binding GUI design system: theming, components, navigation, the agent tool-view contract, settled UI decisions. |
| `ANALYSIS.md` | The note→fact→entity pipeline (extract → Integrator → arbiter), supersession, the review inbox. |
| `entity.md` | The entity & soft-schema model: predicates, facets, names, relationships, resolution. |
| `PREDICATE_CANONICALIZATION.md` | Embedding-assisted predicate registry + typed value shapes (core shipped; self-improvement loop deferred). |
| `ASSISTANT.md` | The self-improving agent design — the Phase-4 core (shipped) plus the deferred loops 2–4 (Phases 5–7). |
| `mocks/` | Interactive HTML UI mockups. `DESIGN.md` cites these as the **binding spec** for reviewed surfaces — a living reference, not throwaway prototypes. |

## Active plan

- `PHASE5_COMPLETION_PLAN.md` — the buildable plan for **finishing Phase 5**
  (reflexion in the live turn, the fed eval harness, the last reconciler, the
  nits, doc hygiene) plus the explicit Phase-6 deferrals. The current frontier.
- `WORKFLOW_ENGINE_PLAN.md` — the (now-complete) build record for the Phase 5
  workflow engine + cutover. Superseded by `PHASE5_COMPLETION_PLAN.md`; to be
  moved to `archive/` in the Phase-5 close-out.

## Archive (history, not active)

`archive/` holds completed build plans and the design research that fed them.
Kept for the audit trail; not the place to learn the current system.

- `archive/ASSISTANT_PLAN.md` — the Phase-4 agent build plan (completed).
- `archive/INTEGRATOR_PLAN.md` — the v3 note→graph pipeline build plan (completed).
- `archive/CUTOVER_V1_REMOVAL.md` — the v1 `analyze_note` removal record (completed).
- `archive/research/` — design-research dossiers (self-improving agent, tool-use
  UX, session-panel UX, subject/object grammar, extraction fix-options).
- `archive/ui-exploration/` — early icon and entity-graph view explorations.

Still-open items from the archived plans are carried forward in `ROADMAP.md`
(Phase 5) so nothing is lost by archiving.
