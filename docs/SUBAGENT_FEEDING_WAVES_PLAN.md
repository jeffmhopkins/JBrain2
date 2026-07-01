# Sub-agent feeding waves — build plan (proposed)

**Status: proposed — design-complete; all open decisions D1–D5 resolved by the owner
(§ Open decisions), ready to decompose into build waves F1–F3.** Extends the shipped
`spawn_subagent` fan (`docs/SUBAGENT_SPAWNING_PLAN.md`, Waves S1–S4) so a single
spawn call can run an **ordered sequence of disconnected waves**, feeding each
wave's summaries forward into the next wave's briefs. This closes the
producer→consumer foot-gun observed live: dependent children fanned in parallel
with briefs that *reference* an upstream task ("given the JSON commit list from
task A") but cannot *access* its output, so they run empty and the parent must
re-spawn them by hand.

This builds under `docs/PROCESS.md` (parallel tasks, per-task + per-wave
adversarial review, one PR per wave, the GUI mock gate) and honours the
`CLAUDE.md` non-negotiables and `docs/ASSISTANT.md` agent design. **It touches the
data/instruction boundary (non-negotiable #1) and the depth≥1 laundering guard
(SUBAGENT_SPAWNING_PLAN decision #7), so it is security-critical and every wave is
red-team gated.**

## The idea in one paragraph

Today `spawn_subagent` is a **flat fan**: one call → N web-sandboxed children run
concurrently (`asyncio.gather`), each in an isolated fresh context, none able to
see another's work, all summaries returned to the parent at once (`spawn.py`
`spawn_fan`). This plan adds an **ordered, disconnected wave** shape: the same call
carries several fans that run **strictly in sequence with a barrier between them**.
Each wave is exactly today's flat fan (parallel, isolated, capped). Between waves,
the tool **feeds** selected upstream children's summaries **into the briefs of the
downstream children that name them** — as **data-framed template slots wrapped in
the explicit data/instruction boundary**, never free-text concatenation. The waves
stay "disconnected" (no child ever sees a sibling's live context; it only receives
finished summaries as boundary-wrapped *data*); the parent still gets the full
roster and synthesizes at the end. The research→review→summarize pipeline becomes
one call the tool sequences and feeds — not a manual two-call round trip.

## Why this shape (the lean litmus)

- **Reuses everything.** A wave *is* the existing fan; feeding *is* the existing
  template-brief data slot (`briefs.py`); the barrier is a plain `await` between
  `asyncio.gather` calls. No new datastore, no scheduler, no framework runtime.
- **Strengthens the boundary instead of weakening it.** Feeding forces a fed
  child's brief to be **template-bound even at depth 0** (today depth-0 briefs are
  free text), so upstream output — which may contain untrusted web content — can
  only ever land in a declared, data-framed slot. The feature is a *forcing
  function* for invariant #1, not an exception to it.
- **Stays short of the framework runtime `ASSISTANT.md` refuses.** No arbitrary
  DAG, no cycles, no per-node retry/branch logic — a bounded, shallow, ordered
  list of waves with forward-only feeding. `MAX_WAVES` keeps it a small feature,
  not LangGraph.
- **The durable-orchestration home already exists** (Phase-5 workflow engine:
  `events→triggers→pipelines→runs`) for anything heavier; this stays the *live,
  in-request, one-turn* escape hatch it was designed to be — deliberately not built
  on the scheduler's `TaskRunner` (which is "neither concurrent nor awaited
  in-request", `spawn.py:163`).

## What "disconnected waves + feeding" means precisely

1. **Disconnected.** No child shares context, memory, or live parent access with
   any other child (unchanged: children are `no_memory`, empty read scope, fresh
   loop). A wave boundary is a hard barrier — wave *k+1* does not start until every
   child in wave *k* has settled.
2. **Feeding = summary-as-data, forward only.** A downstream child receives the
   **finished summary text** of the specific upstream children it names, injected
   into a data-framed slot of its template brief and wrapped in the data/instruction
   boundary the system prompt declares non-executable. It receives no tool logs, no
   reasoning trace, no live stream — only the same summary the parent would read.
3. **Fail-closed.** A downstream child whose fed upstream produced **no usable
   answer** (failed, timed out, or empty) is **skipped, not run over an empty
   block** — surfaced as `[SKIPPED: upstream <label> unavailable]`. Skips cascade
   forward. The call never raises; the parent synthesizes what completed.

## Schema (the spawn_subagent tool, version 3 → 4)

Backwards compatible: a plain `tasks: [...]` call is a single wave, unchanged. The
staged form is additive. **The exact surface is an open decision (§ Open decisions
D1)** — the design is written against the recommended shape:

```jsonc
// Recommended (D1-A): an explicit ordered array of fans; each task may `feed`
// from any label in an EARLIER wave. Ordering and the barrier are self-evident.
{
  "waves": [
    [ { "persona": "research", "label": "fetch-history",
        "brief": "Retrieve the full commit log for <repo> ..." } ],

    [ { "persona": "review", "label": "feature-timeline",
        "feed": ["fetch-history"],
        "brief": { "template_id": "review",
                   "params": { "standard": "...", "deliverable": "..." } } },
      { "persona": "review", "label": "process-audit",
        "feed": ["fetch-history"],
        "brief": { "template_id": "review", "params": { ... } } } ],

    [ { "persona": "summarize", "label": "final-report",
        "feed": ["feature-timeline", "process-audit"],
        "brief": { "template_id": "summarize", "params": { ... } } } ]
  ],
  "max_parallel": 4        // still per-wave concurrency, clamped to MAX_PARALLEL
}
```

- A **fed** task (`feed` non-empty) **must** use a template-bound brief
  (`{template_id, params}`) — validated up front. Its `feed` labels must all resolve
  to tasks in a **strictly earlier** wave (no same-wave or forward feed; no cycles
  are even expressible). A fed label naming a non-existent or later task → the whole
  call is refused with an actionable message (like every other spawn refusal).
- An **un-fed** task keeps today's rules (free text allowed at depth 0).
- **Guard against the observed bug:** a task whose brief text references a sibling
  label or "provided below / above / task A / the same list" **but declares no
  `feed`** is refused with guidance to either inline the data or add a `feed` edge.
  (Cheap, high-value; ships even if D1 lands differently.)

### How feeding is injected (the security-load-bearing step)

The tool composes a single **boundary-wrapped feed block** from the named upstream
summaries and binds it into the fed task's brief as **data**:

```
<untrusted_external_data source="upstream-subagents">
## fetch-history (research)
<verbatim summary text>
</untrusted_external_data>
```

This reuses the depth≥1 template mechanism (`briefs.py` `render_brief`, whose slots
already frame values as "content, not instructions") and the same
`<untrusted_external_data>` envelope the rest of the agent uses for non-authored
content. The model authors none of the feed block; the tool assembles it from
finished summaries. **Injection target is open decision D2** — a dedicated
prepended feed section (recommended) vs. binding into an existing template slot
(`context` / `artifact` / `material`).

## Backend changes (a list, not "just the schema")

1. **`spawn_subagent.tool`** — add `waves` + per-task `feed`; bump `version: 3 → 4`
   (CI digest-pin guarded). Body prose: when to stage waves vs. a flat fan; feeding
   is data, not instruction.
2. **`SpawnService.spawn_fan` → a wave scheduler** (`spawn.py`). Validate all waves
   + all `feed` edges up front (reject the whole call on the first bad edge, as
   today). Mint and announce **every** child across **all** waves up front (later
   waves show "queued · wave N"). Then loop waves in order: `asyncio.gather` the
   wave's runnable children, barrier, compose feed blocks for the next wave from the
   settled results, skip-cascade any child whose feed is unavailable, continue.
   Return one `_observation` + `subagent_synthesis` view over the whole roster
   (grouped by wave), exactly one tool result to the parent.
3. **`briefs.py`** — a `compose_feed_block(labels, results)` helper that renders the
   boundary-wrapped block and binds it into the template brief; strict + fail-closed
   like `render_brief`.
4. **`tree.py`** — add `MAX_WAVES` (§ D3). Keep `MAX_CHILDREN_PER_PARENT` as the cap
   on **total** tasks across all waves in one call, and `MAX_TOTAL_AGENTS_PER_TREE`
   admitting every child up front. Budget: admit the whole staged set against the
   total cap up front, then **re-check `can_admit_budget` at each wave start** so a
   drained pool skips (never silently truncates) a later wave — surfaced explicitly
   (the "no silent caps" rule).
5. **Run-log lineage** — each wave's children keep `parent_run_id = <root run>`
   (unchanged); the child session already carries `parent_session_id`. Add the
   child's `wave` index to the spawned-event payload (below) for the UI; no new
   table.
6. **`jerv.prompt`** (version bump, digest-pinned) — teach jerv to express a
   producer→consumer pipeline as staged waves with `feed`, and to prefer a flat fan
   for genuinely independent breadth. This directly retires the manual re-spawn
   pattern that caused the observed double-run.

## Live surface & session tree (GUI — triggers the mock gate)

- **Events** (`contracts.py`): `SubagentSpawnedEvent` gains a `wave: int`; a fed or
  skipped child carries `fed_from: list[str]` / a `skipped` reason on
  `SubagentDoneEvent`. Later-wave children stream as "queued · wave N" until their
  wave starts (reuses the existing mint-up-front-then-flip-phase pattern,
  `spawn.py:262`).
- **`subagent_synthesis` view** — group the roster by wave, render the feed edges
  (a small "← fed by fetch-history" affordance) and the `[SKIPPED]` state distinctly
  from `[FAILED]`.
- **GUI gate (`PROCESS.md`):** this is a changed GUI surface → **three interactive
  mock HTMLs** in `docs/mocks/` presented to the owner to choose before build. The
  existing `docs/mocks/subagent-chat-mock.html` is the starting point.

## Non-negotiables it must respect (red-team surface for every wave)

- **#1 data/instruction boundary.** Fed summaries enter only inside
  `<untrusted_external_data>` in a declared template slot; never as steering prose.
  A red-team test injects a prompt-injection payload into an upstream summary and
  asserts the downstream child does not act on it.
- **Decision #7 (laundering).** Feeding is the exact hop #7 hardened; requiring a
  template-bound brief for any fed task keeps a fetched page from becoming a
  downstream child's instructions. Depth≥1 stays template-bound regardless.
- **#8/#5 least privilege.** Waves change *ordering and data flow only* — never a
  child's tools or scope. The parent⊆child clamp and empty read scope are untouched.
- **#10 owner-initiated.** Still refused outside an interactive owner turn
  (`ctx.tree is None` guard); no wave runs between turns; nothing persists.
- **No silent caps.** A wave skipped for budget/failure is reported in the
  observation + view, never dropped quietly.

## Testing

- **Unit (adapter-fake driven, deterministic):** a 3-wave feed pipeline runs in
  order; wave *k+1* starts only after wave *k* settles; a fed child's brief contains
  the upstream summary inside the boundary; an un-fed child's brief does not.
- **Fail-closed:** an upstream failure/timeout/empty → its consumers are `[SKIPPED]`,
  not run; skip cascades; the call still returns a roster and the parent synthesizes.
- **Validation/refusal:** fed task without a template brief; `feed` naming a
  later/same-wave or missing label; a sibling-referencing brief with no `feed`;
  `> MAX_WAVES`; `> MAX_CHILDREN_PER_PARENT` total — each a clean refusal, not a crash.
- **Caps/budget:** total-agents admitted across waves; a drained pool skips a later
  wave with an explicit note; cumulative wall-clock bounded.
- **Security/red-team:** injection-in-summary does not steer a fed child; feeding
  cannot widen a child's tools/scope; depth≥1 feed stays template-bound.
- **Sidecar/version:** `spawn_subagent` v4 digest pin; `jerv.prompt` bump pinned.
- Coverage gates unchanged (80% backend, security paths 100%).

## Wave split (build sequence, per PROCESS.md — continues the S-series as F1–F3)

- **Wave F1 — Feeding core + fail-closed scheduler** *(backend; security/red-team
  gated).* Schema (`waves`/`feed`), the wave scheduler in `spawn.py`, `briefs.py`
  feed composition + boundary wrap, the "fed task must be template-bound" +
  sibling-reference guard rules, `MAX_WAVES`, the skip-cascade, tool v4 bump, and the
  full unit + validation + fail-closed + red-team test set. Ships behaviour with the
  *old* flat-fan view (no GUI yet). **This is the load-bearing, security-critical
  wave** — the per-wave gate is a security review of the boundary + laundering
  surface.
- **Wave F2 — Budget/runtime across waves** *(backend; budget-value escalation).*
  Per-wave budget re-admission, cumulative wall-clock handling, run-log/event `wave`
  lineage, the "queued · wave N" live states end-to-end (events only), tests.
- **Wave F3 — Staged synthesis surface** *(GUI; mock gate).* Three interactive mocks
  → owner picks; `subagent_synthesis` grouped-by-wave with feed edges + `[SKIPPED]`;
  `jerv.prompt` bump so jerv actually reaches for staged waves. Verified against a
  live run of the timeline task that misfired.

Each wave: independent worktrees off a `wave-F{n}` branch, per-task adversarial
review (reviewer ≠ builder), a per-wave red-team gate, one PR per wave, CI green
before merge, then proceed.

## Open decisions (owner sign-off before F1 — PROCESS.md critical decisions)

- **D1 — Schema shape. [DECIDED: A — explicit ordered `waves` array.]** Per-task
  `feed` labels reference an earlier wave; the barrier is self-documenting. The
  rejected alternatives were (B) flat `tasks` with per-task `needs: [labels]` and
  waves *derived* by topological leveling, and (C) per-task `wave: int` + `feed`.
- **D2 — Injection target. [DECIDED: A — dedicated prepended section.]** The tool
  prepends a boundary-wrapped `<untrusted_external_data source="upstream-subagents">`
  block above the rendered template brief, keeping the fed data visibly separate from
  the task instructions. (Rejected: binding into an existing `context`/`artifact`/
  `material` slot.)
- **D3 — `MAX_WAVES`. [DECIDED: 4.]** One spawn call may chain up to **4** sequential
  waves (e.g. gather → analyze → cross-check → synthesize). The cap applies **per
  spawn call at any depth**; the existing `MAX_DEPTH`, `MAX_CHILDREN_PER_PARENT`, and
  `MAX_TOTAL_AGENTS_PER_TREE` caps still bound the whole tree, and cumulative
  wall-clock stays governed by the per-child clock × the wave count.
- **D4 — Nesting. [DECIDED: allow nested waves.]** A depth≥1 child may **also** stage
  feeding-waves, not just the depth-0 owner turn. This is safe because depth≥1 briefs
  are **already** template-bound (decision #7), so a fed block is data-framed there by
  construction; the wave scheduler is depth-agnostic. The compounding of `MAX_WAVES`
  with nesting is bounded by `MAX_DEPTH=2` and `MAX_TOTAL_AGENTS_PER_TREE=12`.
- **D5 — Budget admission. [DECIDED: per-wave re-admission.]** Total-agents are
  reserved up front; the token pool is **re-checked at each wave start** against
  `MIN_VIABLE_CHILD_BUDGET` per child, and a wave whose pool is drained is **skipped
  with an explicit note** (never silently truncated). A heavy early wave may thus draw
  from what a cheap later wave would have used, and the later wave skips loudly.

## Deferred past v1

- Conditional/branching waves (run wave *k+1* only if a predicate over wave *k*
  holds) — that is the DAG engine the lean litmus refuses; if ever needed it belongs
  in the workflow engine, not the chat hatch.
- Parent mid-pipeline checkpoint (let the parent inspect/approve between waves) —
  the two-call pattern already provides this; only build if a real need appears.
