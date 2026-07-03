# JBrain2 — Development Standards

> **Status:** Living · **Last verified:** 2026-07-03

These standards bind human and AI contributors equally. CI is the gatekeeper:
lint, typecheck, and tests must be green before merge — no exceptions.

## The architectural constitution

Three rules with no carve-outs:

1. **Every LLM call goes through the adapter.** No direct provider SDK usage
   outside the adapter package.
2. **Every file read/write goes through the storage abstraction.** No direct
   filesystem paths in application code.
3. **Every database query runs on an RLS-scoped session.** No raw connections
   that bypass the domain-scope GUC.

Layering: routes → services → repositories. No SQL in route handlers. Schema
changes only via Alembic migrations, written reversible unless impossible.

**Prompts live in co-located `.prompt` files, not in code.** An LLM prompt is one
artifact — prose, output JSON schema, token budget, capability tier (`strength:`
— high/low/vision/embedding, resolved to a concrete model by the adapter, never a
model id), and a `version` — in YAML frontmatter + a templated body, loaded by
`jbrain.llm.promptfile` beside the module that uses it (e.g.
`analysis/prompts/note_extract.prompt`). The version is stamped on every record
the prompt produces, so a CI guard fails if the prose changes without a version
bump (a re-run is then a deliberate migration). Every prompt lives this way
(e.g. `note.extract`, `entity.disambiguate`, `vision.ocr`, `vision.caption`, and
the wiki/intake/agent prompts added since); a new prompt is a new `.prompt` file,
never an in-code string, and tool definitions adopt the same sidecar pattern
(`.tool` files, with a matching version-bump CI guard).

## Comment standards

Comments explain **why**, never **what** — names and types carry the what.
Density is deliberately lean: AI agents and humans both navigate typed code
better than narrated code, and stale comments are worse than none.

- **Python**: type hints required on all function signatures. Google-style
  docstrings on public modules, classes, and functions — one summary line
  always; Args/Returns/Raises only when non-obvious. Trivial private helpers
  need none.
- **TypeScript**: TSDoc on exported functions, hooks, and components only
  where behavior isn't evident from the signature.
- **Inline comments** are reserved for: non-obvious constraints (e.g. "RLS
  requires this GUC set before any query"), workarounds (with a link to the
  upstream issue), and domain rules (e.g. "superseded facts stay queryable
  for citation integrity").
- `TODO(topic): description` — every TODO references a tracked issue or is
  resolved within the PR.
- **No commented-out code in commits.** Git is the archive.

## Code standards

### Python
- **Ruff** for linting and formatting (replaces black/isort/flake8).
- **Pyright** for type checking; public APIs fully typed.
- **pydantic-settings** for config; env vars only; no secrets in the repo.
- Typed exception hierarchy; no bare `except`; structured logging
  (structlog) with request/job IDs.

### TypeScript
- **Biome** for linting and formatting.
- `strict: true`; no `any` without an inline justification comment.
- API client types generated from the FastAPI OpenAPI schema — frontend and
  backend cannot drift.

## Git workflow

- **Branch + PR always**, even solo: short-lived branches, merged only with
  CI green. This buys CI gating, AI review passes, and clean revert points.
- **Conventional Commits**: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`,
  `chore:`.
- No force-pushes to `main`.
- **Docs reconciled in the same PR** (per `docs/DOC_LIFECYCLE.md`): plan status
  flipped or archived when its waves land, Living docs corrected when behaviour
  changes, and `Last verified` bumped. Run `scripts/docs-freshness.sh` first.

## Releases

- Two channels: **stable** (images built on release tags, semver) and
  **edge** (images built on every green `main` commit).
- A release tag is cut deliberately, never automated from merge.
- Every release must be reachable by the supervisor's update sequence:
  schema migrations ship in the same release as the code that needs them,
  and migrations must tolerate the previous release's running code during
  the rolling restart window.

## Development environment

`scripts/dev-setup.sh` is the **single source of truth** for bootstrapping a
dev environment from a fresh checkout: Python deps via `uv sync`, frontend
deps via `npm install`, and a Docker availability check. It is idempotent
and phase-aware (skips parts of the project that don't exist yet).

- **Any PR that adds a dependency, tool, or setup step must update
  `scripts/dev-setup.sh` in the same PR** — the same rule as tests-with-code.
- Environments without a Docker daemon (e.g. Claude Code web sessions) can
  run linters and unit tests but not testcontainers integration tests; the
  test suite must skip those cleanly (pytest marker + docker availability
  check), never fail or hang.

## Testing requirements

### Tooling
- Backend: **pytest + pytest-asyncio**. Integration tests run against **real
  Postgres via testcontainers** — never SQLite, never mocked sessions.
- Frontend: **Vitest + React Testing Library**. Later: a thin Playwright
  smoke suite (login → create note → search finds it).

### Coverage gate
- Backend: CI fails below **80% line coverage**.
- **Security-critical paths require 100%**: RLS policies, auth, capability
  tokens, device keys, domain scoping. Every new table ships with an RLS test
  proving a scoped session cannot read other domains' rows.

### Rules
- **Tests land in the same PR as the code they cover.** A PR without tests
  for its new behavior does not merge.
- **Every bugfix includes a regression test** that fails before the fix.
- **LLM calls never run in tests.** Use the adapter's fake implementation
  with canned responses. Prompt-quality evaluation is a separate,
  deliberately-run eval suite outside CI.
- Tests are deterministic: no network, no real clock (inject time), no
  ordering dependence. The suite stays fast enough to run on every commit.

### What gets unit tests vs integration tests
- Pure logic (chunking, RRF fusion, supersession resolution, triage parsing):
  unit tests, no I/O.
- Anything touching Postgres, RLS, the queue, or storage: integration tests
  via testcontainers.
- API surface: httpx against the FastAPI app.
