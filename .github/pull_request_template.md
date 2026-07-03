## What & why

<!-- One or two sentences: what this change does and why. Link the plan/issue. -->

## Checklist

- [ ] **Tests land with the code** — new behaviour covered; every bugfix has a regression test; new tables ship an RLS isolation test. LLM calls faked. (`CLAUDE.md` #3, #5)
- [ ] **Non-negotiables honoured** — LLM calls via the adapter, file I/O via storage, DB on an RLS-scoped session. (`CLAUDE.md` #1–#3)
- [ ] **`scripts/dev-setup.sh` updated** if this adds a dependency, tool, or setup step. (`CLAUDE.md` #8)
- [ ] **Docs reconciled in this PR** (`docs/DOC_LIFECYCLE.md`, `CLAUDE.md` #9): plan status flipped or archived when its waves land; Living docs corrected when behaviour changes; `Last verified` bumped; a new doc filed by kind (`reference/`/`runbooks/`/`plans/`); no volatile counter hardcoded in prose. Ran `scripts/docs-freshness.sh` (green).
- [ ] **CI green before merge** — lint, typecheck, tests + coverage gates, and the `docs` gate. (`CLAUDE.md` #6)
