# JBrain2 — Multi-wave execution process

> **Status:** Living · **Last verified:** 2026-07-03

The **binding process** for building a phased plan (e.g. `WORKFLOW_ENGINE_PLAN.md`)
once its waves are defined. It governs *how* the work is sequenced, reviewed, and
landed — and it binds human and AI contributors equally, on top of
`docs/reference/DEVELOPMENT.md` (the standards) and the `CLAUDE.md` non-negotiables.

## The loop, per wave

A wave is a set of tasks that can run mostly in parallel. For each wave:

1. **Parallelize.** Run the wave's tasks concurrently — independent agents in
   isolated git worktrees off a single `wave-N` integration branch. Maximize
   overlap; only true dependencies serialize.
2. **Per-task gate.** When a task is done, an **independent adversarial review**
   (a *different* agent than the one that built it) reviews the diff against the
   plan, the codebase, and the invariants. Findings are fixed on the task branch
   before it merges into the wave branch. Reviews and fixes are **silent** unless
   a finding is a critical decision (below).
3. **Per-wave gate.** After all tasks land on the wave branch, a **second,
   wave-level adversarial review** reads the whole wave diff (correctness +
   reuse + security/red-team for any RLS/firewall/scope-touching wave). Fix on
   the wave branch.
4. **One PR per wave.** Open **exactly one PR per wave**, and only **after the
   wave is complete and both review gates are clean** (no draft-PR-at-start). The
   PR is the first CI signal for the wave — so each task must be locally verified
   (lint + typecheck + unit tests) before it merges into the wave branch, since
   testcontainers/integration tests run only in CI here.
5. **Green, then merge, then proceed.** Wait for CI green, merge, and
   **automatically begin the next wave** — no check-in required.

## Verification levels

- **Per task (local):** `ruff` + `pyright` (or `biome`/`tsc` for frontend) + the
  task's unit tests, run before merging into the wave branch.
- **Per wave (CI, at the PR):** the full suite — lint, typecheck, testcontainers
  integration tests, coverage gates (80% / security-100%), `.prompt`/`.tool`
  digest pins, `dev-setup.sh` currency, and `scripts/docs-freshness.sh` (the
  `docs` job — enforces `docs/DOC_LIFECYCLE.md`). CI must be green before merge.

## Communication

- **Update the owner only on:** task **start**, task **completion**, and **wave
  status** (wave start, wave PR opened, wave merged). Nothing else is narrated.
- **Escalate to the owner (critical decisions only):**
  - **Architectural / security findings** — a change touching RLS, the domain
    firewall, principal scope, the data/instruction boundary, or a cross-cutting
    refactor.
  - **Plan open decisions** — the deferred `§7`-style choices (e.g. runs-vs-jobs
    boundary, cron representation, scheduler concurrency, budget values).
  - **Scope deviations** — a task proves materially bigger/different than the
    plan, or a planned item proves unnecessary.
  - **A PR that won't go green** after re-diagnosis (don't spin silently).
  - **The GUI gate** (below).
- A new **runtime dependency** is avoided by default (zero-new-dep goal); if one
  becomes unavoidable it is flagged in the wave status, not treated as a stop.

## GUI gate

Any task that adds or changes a **GUI surface** requires **three interactive mock
HTML artifacts** (real, clickable, per `docs/reference/DESIGN.md`'s mock-first discipline)
**presented to the owner to choose the path before implementation begins**. The
chosen mock becomes the binding spec and lands in `docs/mocks/`. This is a
critical-decision interruption by design.

## Delegation

Make **heavy use of independent agents**: researchers for codebase grounding
before a wave; builders per task; and **independent reviewers / red teams** for
every per-task and per-wave gate. Review independence is non-negotiable — the
reviewer is never the builder.
