# JBrain2 — Multi-wave execution process

> **Status:** Living · **Last verified:** 2026-10-17

The **binding process** for building a phased plan (e.g. `docs/archive/WORKFLOW_ENGINE_PLAN.md`)
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
- **Per wave (CI, at the PR):** lint + typecheck, the unit and testcontainers
  integration suites (which carry the `.prompt`/`.tool` digest-pin tests), the
  combined **80%** backend coverage gate, and `scripts/docs-freshness.sh` (the
  `docs` job — enforces `docs/DOC_LIFECYCLE.md`). PR runs are path-filtered by
  the `changes` job — untouched areas post green in seconds; pushes to `main`
  run everything. Security-100% and `dev-setup.sh` currency are review-enforced
  standards, not CI jobs. CI must be green before merge.

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

## Review: the session reviews its own PRs

**The PR's own session reviews it, with review agents it spawns** (owner, 2026-10-17:
*"Update to not use remote reviewer, but to spawn off from your session"*). The remote
reviewer agent (owner, 2026-10-12) is no longer the gate: no session waits for it, and
merging no longer waits 10 minutes for its comment. Instead:

- **Before merging, spawn a reviewer on the PR's current head** (the Agent tool, a fresh
  agent with no stake in the change). Give it the diff (`git diff origin/main...HEAD`),
  the PR description, and the plan or doc the change implements, and have it look for
  correctness bugs, security holes (this repo's non-negotiables: RLS, the storage and LLM
  abstractions, the domain firewalls), missing tests and docs drift. A security-relevant
  change also gets a second agent focused only on the security path.
- **Fix what it finds in the same PR, or say why not.** A real finding is fixed and
  pushed; a finding you judge wrong is answered in the PR (one line on the path you
  traced). A push that changes code is a new head: review the new diff before merging.
- **Merge only when CI is green and the review is clean.** Merge the latest main into the
  branch right before merging, then let CI run on that head. Two PRs can each pass alone
  and still break main together: on 2026-10-12 two migrations both numbered `0236` merged
  minutes apart, main had two alembic heads, and every deploy failed until a renumber
  landed.
- **Read the PR's comments and reviews anyway.** If anyone (a person, a bot, the old
  reviewer if it still runs) leaves a finding on your PR, it is yours to fix there.

### Issues that name your PR

If a GitHub issue names a PR you opened ("Introduced in #N"), it is yours. Check the open
issues at the start of a session and before you report a stretch of work as done.
- **Keep the issue current while you work on it** (owner, 2026-10-12): a comment when
  you start ("fixing in this session"), one when the fix PR is open (link it), and the
  closing comment once it's merged, deployed and verified. Whoever reads the issue sees
  where it stands without asking.
- **Issue checks never block.** Creating, merging and deploying a PR doesn't wait for
  them; anything they find is fixed as a new PR. List the open issues and match each one's
  "Introduced in" / "while reviewing" PR against the PRs this session (or the work you
  continued) opened.
- **One that names your PR is yours:** fix it the usual way — a failing test first, the
  fix, `Fixes #N` in the commit, merge, deploy, verify on the box, then a one-line
  closing comment saying what was wrong and which PR fixed it.
- **One that names another session's PR** is that session's; mention it to the owner
  rather than fixing it, unless the owner says otherwise.
- **A finding you judge wrong** gets a reply on the issue with the path you traced and
  why, and the owner decides; never close it silently.

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
