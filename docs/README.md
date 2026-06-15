# JBrain2 — Documentation map

JBrain2 is a personal knowledge system: notes in → RAG indexing → an
LLM-maintained wiki with notes as the sole sources of truth. This folder holds
the binding design docs. Project-wide non-negotiables live in the root
`CLAUDE.md`.

## Where the project is (2026-06)

**Phases 0–4 are shipped** — note capture, ingestion/search, the v3 note→graph
analysis pipeline, and the personal agent (tool-calling loop, Tier-A memory,
Proposals/review inbox, external connectors, the Full Brain chat surface), plus
lists and appointments. Migrations run through 0034.

**Next: Phase 5 — the workflow engine + eval harness.** See `ROADMAP.md` for the
phase status and the items carried forward out of Phases 3–4.

## Living reference (read these)

| Doc | What it covers |
|---|---|
| `ARCHITECTURE.md` | System shape: containers, the one-database design, the knowledge pipeline, security model, operations. |
| `ROADMAP.md` | Phase plan and current status. The source of truth for "what's next." |
| `DEVELOPMENT.md` | Binding standards: the architectural constitution, comments, testing, git, releases, `dev-setup.sh`. |
| `DESIGN.md` | Binding GUI design system: theming, components, navigation, the agent tool-view contract, settled UI decisions. |
| `ANALYSIS.md` | The note→fact→entity pipeline (extract → Integrator → arbiter), supersession, the review inbox. |
| `entity.md` | The entity & soft-schema model: predicates, facets, names, relationships, resolution. |
| `PREDICATE_CANONICALIZATION.md` | Embedding-assisted predicate registry + typed value shapes (core shipped; self-improvement loop deferred). |
| `ASSISTANT.md` | The self-improving agent design — the Phase-4 core (shipped) plus the deferred loops 2–4 (Phases 5–7). |
| `mocks/` | Interactive HTML UI mockups. `DESIGN.md` cites these as the **binding spec** for reviewed surfaces — a living reference, not throwaway prototypes. |

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
