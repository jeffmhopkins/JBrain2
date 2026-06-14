# Engine authoring standard + the promotion path

**Role:** design dossier — resolves two of the README's open items: the DBOS
authoring standard (adoption conditions #1/#2) and the agent-skill → standing-
pipeline promotion path (the one open design seam).
**Date:** 2026-06-14
**Status:** proposed. Kept here (not in `docs/DEVELOPMENT.md`) because DBOS is not
yet a committed dependency; Part 1 graduates into `DEVELOPMENT.md` and Part 2 into
`ASSISTANT.md`/the Phase-5 plan when DBOS lands. Collision-free with the concurrent
ingestion→entity-graph session.

---

## Part 1 — Workflow authoring standard (conditions #1 and #2)

This is the binding standard for any code under `jbrain.workflow` once DBOS is
adopted. It is what keeps DBOS's two sharp edges (determinism footgun; system-DB
outside RLS) from becoming defects. The spike (`workflow/spike.py`,
`workflow/safety.py`) already demonstrates every rule.

### Determinism (condition #2)
1. **The workflow body is deterministic.** It may sequence steps, branch on their
   results, and loop over their results — nothing else. Same inputs → same sequence
   of step calls.
2. **Every nondeterministic effect is a `@DBOS.step`.** That means *every*
   LLM-adapter call, *every* SQLAlchemy query, *every* storage-abstraction read, and
   any clock/UUID/random/network read. If it can return a different value on replay,
   it is a step. A workflow body that calls `datetime.now()`, the LLM adapter, or the
   DB directly is a bug, not a style nit.
3. **Steps are idempotent.** DBOS retries an incomplete step at-least-once, so a
   step's external effect must be safe to repeat: DB writes upsert or no-op on
   replay, keyed by `run_id`/a natural key; never "insert a duplicate."
4. **Async concurrency is the trap.** Concurrent steps inside one workflow must not
   introduce ordering nondeterminism; prefer DBOS queues (deterministic handles) over
   ad-hoc `asyncio.gather` over steps.

### IDs not payloads (condition #1)
5. **Workflow/step arguments and return values are reference-shaped** — IDs,
   handles, enum values, short identifiers, and containers thereof; never note,
   chunk, or LLM-output bodies. Enforced by `workflow.safety.assert_reference_shaped`.
6. **Content is re-fetched, never carried.** A step that needs a note body fetches
   it through an RLS-scoped session *inside the step* and returns a reference to its
   result, so firewalled content never serializes into the `dbos` schema.
7. **Every workflow ships an integration test** that runs it against a
   testcontainers Postgres and asserts (a) it completes and (b) no persisted
   input/output is content-shaped (the `test_no_firewalled_payload_in_system_schema`
   pattern). LLM calls are faked per the existing adapter convention.

### Schema & deploy boundary (conditions #3/#4 — stated, enforced at adoption)
8. **Alembic owns `public`; `dbos migrate` owns `dbos`.** No app migration touches
   the DBOS schema; `dbos migrate` runs as a deploy step beside `alembic upgrade
   head`; the version is pinned.
9. **Long-paused workflows survive deploys.** Because recovery is gated on a
   workflow-source hash, any deploy while an approval is pending uses `DBOS.patch()`
   or blue-green draining (condition #4).

---

## Part 2 — The promotion path: agent skill → standing pipeline

The two-surface model (README) leaves one seam: a useful thing the agent does
interactively should be able to **graduate** into a standing scheduled/triggered
pipeline. This is the design for that graduation.

### The boundary it crosses (why it needs gates)
An **interactive skill** has a human in the loop on every invocation — the owner is
in the chat, and `mutate`/`sensitive` actions are **staged** for them
(`ASSISTANT.md` §"Session capabilities"). A **standing pipeline** runs
**unattended**: no human is present per run. Promotion therefore crosses an autonomy
boundary, and the binding rule *"writes are never standing — always staged"* must
survive the crossing. The whole design is the answer to: *how does an unattended
pipeline keep that promise?*

### The invariant: an unattended pipeline never auto-mutates
A promoted pipeline may **read** freely within its (single) domain, but any state
change it wants is **staged as a Proposal** for the owner, exactly as an interactive
session would — or gated behind an in-workflow `DBOS.recv` approval (the spike's
durable-pause pattern). So:
- A **read-only** pipeline (e.g. the weekly entity digest, the wiki *analysis* pass)
  may run fully autonomously on a schedule.
- A **mutating** pipeline runs autonomously only up to the point of a change, where
  it **stages a Proposal and stops** (or durably waits on approval). It never applies
  a mutation no human approved.

This mirrors the existing skills rule — *"read-only compositions auto-promote;
mutating/side-effecting skills never auto-promote"* — and extends it from invocation
to scheduling. The autonomy a pipeline gains is **autonomy to run and to read and to
propose**, never autonomy to write.

### Eligibility (what may be promoted)
A skill/composed sequence is promotable when it is:
1. **Verified** — succeeded across the eval harness including a **safety/groundedness
   regression**, not task-success alone (the skill-promotion gate).
2. **Reference-shaped & deterministic** — passes the Part-1 standard (so it *can* be a
   DBOS workflow).
3. **Single-domain** — no cross-domain composition (non-negotiable).
4. **Write-safe** — either read-only, or every mutation is a staged Proposal /
   approval-gated step.

### Mechanism (how promotion happens)
1. **Proposal, not direct.** Promotion is a review-inbox **`pipeline-promotion`
   Proposal** (a new item type alongside `skill-promotion`), staged by the agent or
   the owner. Read-only + eval-passing skills may auto-stage the Proposal; mutating
   ones are owner-initiated. Untrusted-origin content can never stage one
   (non-negotiable).
2. **Approval registers data, not code.** On owner approval, the skill's block
   sequence becomes a **pipeline definition** (the same registered blocks, now under
   a name + version) plus a **trigger** row (a `@DBOS.scheduled` cron or an event
   subscription). No new code ships — the blocks already exist in the registry; only
   the *composition + trigger* is registered.
3. **Versioned and reversible.** A promoted pipeline carries a `version`; demotion is
   disabling its trigger; a bad promotion is reverted like any other definition. Each
   run is a DBOS workflow with a full run log.
4. **Budgeted.** Standing pipelines run under the **hard daily token/cost/job
   budgets** for self-improvement work (non-negotiable #10), separate from
   interactive budgets, and are batched.

### Where it lands
- **Trigger** = a `scheduled_triggers`/event row (or `@DBOS.scheduled`) — the
  unattended clock/event.
- **Pipeline** = the promoted block sequence, run as a DBOS workflow.
- **The write gate** = `set_event` + a staged Proposal in the review inbox (the
  spike's `recv`/`send` pattern), so "autonomous mutation" is structurally
  impossible.

### The one decision to confirm
The above recommends **"unattended pipelines may read and propose, never
auto-write."** The alternative — letting a promoted, eval-gated pipeline apply
*low-risk* mutations autonomously (e.g. tag consolidation) without per-run staging —
would buy convenience at the cost of a standing write authority the security model
currently forbids. Recommendation: **do not** open that door; keep all standing
mutation staged. Flagging it because it is the load-bearing policy choice in this
design.
