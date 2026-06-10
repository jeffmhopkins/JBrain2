# JBrain2

Personal knowledge system: notes in → RAG indexing → LLM-maintained wiki with
notes as the sole sources of truth. See `docs/ARCHITECTURE.md` for the full
design, `docs/ROADMAP.md` for phases, `docs/DEVELOPMENT.md` for binding
standards, `docs/DESIGN.md` for the binding GUI design system, and
`docs/ANALYSIS.md` for the note-analysis pipeline (Phases 2-3).

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
