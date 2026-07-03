# JBrain2

Personal knowledge system: notes in → RAG indexing → LLM-maintained wiki with
notes as the sole sources of truth. See `docs/reference/ARCHITECTURE.md` for the full
design, `docs/ROADMAP.md` for phases, `docs/reference/DEVELOPMENT.md` for binding
standards, `docs/reference/PROCESS.md` for the binding multi-wave execution process,
`docs/reference/DESIGN.md` for the binding GUI design system,
`docs/reference/ANALYSIS.md` for the note-analysis pipeline (Phases 2-3),
`docs/reference/ASSISTANT.md` for the agent design,
`docs/reference/PREDICATE_CANONICALIZATION.md` for the embedding-assisted predicate
registry + typed value shapes (largely superseded by the refocus plan), and
`docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md` for the two-tier predicate model and the
entity-graph refocus (spine, not encyclopedia): the registry is tier-1,
long-tail predicates commit raw with no review card. Phases 0–5 are shipped —
the Phase 5 workflow engine (engine + scheduler + run-log + the
ingest/integration cutover) is complete. The next frontier is Phase 6 (the wiki);
see `docs/ROADMAP.md` for current status. Completed build plans and design
research live under `docs/archive/` (see `docs/README.md` for the full map). How
those docs are kept true — the two doc kinds, the freshness header, and the
archive-on-merge rule — is `docs/DOC_LIFECYCLE.md`, enforced by the `docs` CI gate.

## Non-negotiables for all code in this repo

1. All LLM calls go through the LLM adapter — never a provider SDK directly.
2. All file I/O goes through the storage abstraction — never raw paths.
3. All DB queries run on an RLS-scoped session — domain firewalls (health,
   finance, location) are enforced in Postgres, and every new table needs an
   RLS isolation test.
4. Comments explain why, not what. Lean density. No commented-out code.
5. Tests land in the same PR as the code: 80% backend coverage gate,
   security paths at 100%, real Postgres via testcontainers, LLM calls faked.
6. Conventional Commits; branch + PR always; CI green before merge.
7. The wiki is machine-written only; humans correct it via correction notes,
   never direct edits.
8. `scripts/dev-setup.sh` bootstraps the dev environment (it auto-runs at
   session start on the web) and must be updated in the same PR as any new
   dependency, tool, or setup step.
9. Docs travel with the code (`docs/DOC_LIFECYCLE.md`): every PR reconciles the
   docs it touches — a plan's status flipped or archived when its waves land,
   Living docs corrected when behaviour changes, `Last verified` bumped, and a
   new doc filed by kind (`reference/` / `runbooks/` / `plans/`). Never hardcode
   a volatile counter (e.g. a migration head) in prose. The `docs` CI gate
   (`scripts/docs-freshness.sh`) enforces it.
