# JBrain2 — docs archive

Historical documents: completed build plans and the design research that
informed them. They are kept for the audit trail and to preserve the reasoning
behind shipped decisions. **They do not describe the current system** — for that,
start at `docs/README.md` and the living reference docs beside it.

| Item | What it is | Status |
|---|---|---|
| `ASSISTANT_PLAN.md` | Phase-4 personal-agent implementation plan (P4.1–P4.9). | Completed — agent shipped. |
| `INTEGRATOR_PLAN.md` | Note→graph Integrator (v3) implementation plan. | Completed — pipeline shipped; a few deferrals carried to `ROADMAP.md`. |
| `CUTOVER_V1_REMOVAL.md` | Record of removing the v1 `analyze_note` path. | Completed. |
| `WORKFLOW_ENGINE_PLAN.md` | Phase-5 workflow-engine + cutover build plan. | Completed — superseded by `PHASE5_COMPLETION_PLAN.md`. |
| `PHASE5_COMPLETION_PLAN.md` | Phase-5 residual-completion plan (reflexion in the live turn, fed eval harness + nightly schedule, last reconciler, nits, doc hygiene). | Completed — Phase 5 closed; Loops 2–4 carried to `ROADMAP.md` Phase 6. |
| `research/` | Design-research dossiers (self-improving agent A–G, brain-tooluse-ux, session-panel-ux, subject-object-grammar, fix-options). | Findings baked into the shipped design + the docs above. |
| `ui-exploration/` | Early PWA-icon and entity-graph / search-icon explorations. | Directions chosen; lifted into the app. |

> Note: cross-references inside these archived files may use the docs' original
> pre-archive paths (e.g. `docs/research/...` rather than `docs/archive/research/...`).
> They are left as written to preserve the historical record.
