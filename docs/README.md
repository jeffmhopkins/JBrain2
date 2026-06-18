# JBrain2 — Documentation map

JBrain2 is a personal knowledge system: notes in → RAG indexing → an
LLM-maintained wiki with notes as the sole sources of truth. This folder holds
the binding design docs. Project-wide non-negotiables live in the root
`CLAUDE.md`.

## Where the project is (2026-06)

**Phases 0–5 are shipped** — note capture,
ingestion/search, the v3 note→graph analysis pipeline, the personal agent
(tool-calling loop, Tier-A memory, Proposals/review inbox, external connectors,
the Full Brain chat surface), lists and appointments, and the **workflow engine**
(`events`/`triggers`/`pipelines`/`actions`/`runs` + scheduler + unified run-log +
the cutover of ingest/integration/consolidation onto the engine), reflexion in the
live turn, a fed eval harness (the live scorer + a nightly schedule that stores
`EvalRun`s), and the recurring self-heal reconcilers. Migrations run through 0044.

**Phase 5 is complete; next is Phase 6 (Wiki).** The self-improvement Loops 2–4
(skill learning, durable-knowledge promotion, prompt/tool self-edit) and the
not-yet-built hygiene sweeps are deferred to Phase 6. See `ROADMAP.md`; the completed
Phase-5 build record is `archive/PHASE5_COMPLETION_PLAN.md`.

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
| `STRIX_HALO_SETUP.md` | End-to-end runbook for self-hosting the optional local models on an AMD Strix Halo (Ryzen AI Max+ 395) box: distro → kernel → Vulkan → install → routing. |
| `CLOUDFLARE_TUNNEL.md` | Reaching a home-network box from outside via Cloudflare Tunnel — the dynamic-IP / CGNAT path: no static IP, no port-forwarding, TLS at Cloudflare's edge. |
| `mocks/` | Interactive HTML UI mockups. `DESIGN.md` cites these as the **binding spec** for reviewed surfaces — a living reference, not throwaway prototypes. |

## Active plan

- `PHASE6_WIKI_PLAN.md` — the **Phase 6 (Wiki)** build plan (in progress): the
  machine-written wiki (cross-domain articles, domain-tagged sections, incremental
  nightly builder, correction-note loop, read-only UI). Owner decisions on scope +
  revision storage are settled; remaining gates are the UI mock round and a cross-stream
  citation/delta-feed contract with the entity-graph rebuild. Most of the phase is gated
  on that rebuild; only the article/index shell + UI are parallel-safe now.

## Archive (history, not active)

`archive/` holds completed build plans and the design research that fed them.
Kept for the audit trail; not the place to learn the current system.

- `archive/PHASE5_COMPLETION_PLAN.md` — the Phase-5 residual-completion build plan
  (completed): reflexion in the live turn, the fed eval harness + nightly schedule,
  the last reconciler, the nits, doc hygiene, and the Phase-6 deferrals (incl. the
  Loop-4 self-edit decision).
- `archive/WORKFLOW_ENGINE_PLAN.md` — the Phase-5 workflow-engine + cutover build
  plan (completed); superseded by `archive/PHASE5_COMPLETION_PLAN.md`.
- `archive/ASSISTANT_PLAN.md` — the Phase-4 agent build plan (completed).
- `archive/INTEGRATOR_PLAN.md` — the v3 note→graph pipeline build plan (completed).
- `archive/CUTOVER_V1_REMOVAL.md` — the v1 `analyze_note` removal record (completed).
- `archive/research/` — design-research dossiers (self-improving agent, tool-use
  UX, session-panel UX, subject/object grammar, extraction fix-options).
- `archive/ui-exploration/` — early icon and entity-graph view explorations.

Still-open items from the archived plans are carried forward in `ROADMAP.md`
(Phase 5) so nothing is lost by archiving.
