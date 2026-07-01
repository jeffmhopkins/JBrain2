# Sub-agent spawning — build plan (scheduled)

> **Superseded in part — child-initiated nesting was removed.** This plan built a
> two-layer tree (jerv → child → grandchild). In practice the model would not
> reliably spawn *as a child* even when instructed, and the depth≥1 machinery
> (decision #6 depth cap at 2, decision #7 template-bound grandchild briefs) was an
> unused security surface. So **children are now always leaves** (`MAX_DEPTH = 1`;
> `spawn_subagent` dropped from the research/review personas). Structured
> orchestrator-declared work is expressed by **feeding waves**
> (`docs/SUBAGENT_FEEDING_WAVES_PLAN.md`) instead — and the brief templates that
> decision #7 introduced live on there, now guarding *fed-consumer* briefs rather
> than a grandchild-spawn hop. References below to depth≥1, grandchildren, and "two
> sub-agent layers" are retained as the historical record.

**Status: scheduled — design-complete, decomposed into waves S1–S4 (see "Wave
split").** Lets the web-sandboxed agent (`jerv`) spawn web-sandboxed
**research / review / summarize** sub-agents for context flexibility. Promoted out
of `proposed/` and given a roadmap slot (`docs/ROADMAP.md`, Phase 6 follow-ons —
agent-infrastructure, independent of the wiki spine). Reconciled with the
`CLAUDE.md` non-negotiables and the `docs/ASSISTANT.md` agent design (see
"Reconciliation"). Nothing is built yet; the waves below sequence the build under
`docs/PROCESS.md` (parallel tasks, per-task + per-wave adversarial review, one PR
per wave, the GUI mock gate).

> **Read this first — honesty about what exists.** An adversarial review (security
> + architecture + GUI) found the earlier draft asserted properties as
> *"settled / structural / harness-enforced / reuses-existing"* that the live code
> does **not** enforce today. This revision corrects that: every safety property
> below (the parent⊆child clamp, the depth/fan/budget caps, the no-memory and
> no-location guarantees) is **net-new machinery that the build must add to the
> loop/registry/schema** — it is *not* present in `loop.py` today. Where the design
> needs a loop or schema change, this doc says so. The structural enforcement
> *is the work*; until it ships, none of these guarantees hold.
>
> The full adversarial-review findings (security + architecture + GUI) and their
> resolutions are recorded in `docs/archive/SUBAGENT_SPAWNING_REVIEW.md` — the
> per-wave adversarial reviews should re-check those same surfaces.

## Reconciliation with the reserved `ASSISTANT.md` hatch (owner-approved)

`docs/ASSISTANT.md:170` reserves one hatch, and this spec **materially reinterprets
three of its properties**. The owner has **approved** this reinterpretation (the
fan + web-sandbox + live-streaming reading, below) over the literal one-shot hatch:

> **No standing multi-agent orchestra.** … Keep exactly one narrow `spawn_subagent`
> escape hatch … runs the **same loop with a fresh context and the same RLS-scoped
> tool set, returns only a summary** — context isolation, **not** … privilege
> escalation.

| Hatch property (as written) | This spec | Why it stays within "context isolation, not privilege escalation" |
|---|---|---|
| "same RLS-scoped tool set" | children are **web-sandboxed (no KB)** — a *narrower* tool set | A child can only ever hold ⊆ the parent's tools/scope (structural clamp). Narrowing never escalates. |
| "returns only a summary" | summary return **plus live progress** streamed to the chat/tree | Progress frames are **read-only telemetry**; the only data that re-enters the parent's reasoning is the summary, still wrapped in the data boundary. No new write path. |
| "one narrow hatch" | a **bounded fan** (≤ caps), **two sub-agent layers** deep | Ephemeral (one parent turn), owner-paced (only an owner turn can spawn), structurally capped (depth/fan/budget). Not a *standing* orchestra — nothing persists or runs between turns. |

(Recorded: approved. The literal one-shot, summary-only, single-child hatch was
the rejected fallback.)

## The idea in one paragraph

`jerv` reads a default **brief template** (research / review / summarize), fills it
for the task, and calls `spawn_subagent` to launch a **fan** of one or more child
sessions. Each child runs the **same `AgentLoop`** with a web-sandboxed persona
(no KB), **tools and access clamped to ⊆ the parent**, its own budget slice drawn
from a **shared tree budget**, and a **depth counter** that refuses to spawn past
**two sub-agent layers (three including the main session)**. Children run as
**concurrent in-request tasks** the parent turn awaits (fan-in model A), streaming
live progress to the chat and the session tree via a **new loop event channel**,
and return **only a summary** as data. The parent reads the summaries, cites them,
and composes the final answer.

## Settled decisions (owner)

1. **Execution model: detached-feel + live, fan-in model A.** Children run as
   concurrent tasks the parent turn awaits; they stream live progress into the chat
   and the session tree. (See "Execution model" for why this is in-request
   `asyncio.gather`, not the scheduler's Tasks runner.)
2. **Live in-chat panel ships in v1** — accepting that v1 must add the loop
   **ChatEvent channel** (below) so children can stream into the parent bubble.
3. **Sub-agent access: web-sandboxed (jerv-style)** — research/review/summarize read
   **no knowledge base**; web + transform only, returning cited summaries.
4. **The spawner is `jerv`,** not `curator`. `curator` holds no web tools, so a
   web-research child could not inherit web access from it; `jerv` is the only
   coherent root. (Enforced structurally — see "Structural enforcement" — not by
   prose: `curator.tools is None` is a *superset*, so `spawn_subagent` must be
   explicitly excluded from the wildcard.)
5. **Children never exceed the parent** — tools and scope always ⊆ parent
   (∩ the child persona's allowlist), enforced at dispatch.
6. **Nesting depth: spawn allowed iff `parent.depth < 2`** (root = depth 0; depths
   0 and 1 may spawn; a child is created at depth ≤ 2; depth-2 is a leaf).
7. **Briefs are template-bound at depth ≥ 1.** A child that can read the web may
   only spawn a grandchild with a **structured template brief (filled fields, no
   free-text prose)** — so attacker-controlled fetched content cannot be laundered
   into a grandchild's instructions (closes the data/instruction-boundary hole at
   the re-spawn hop). Only the depth-0 owner turn may compose a free-text brief.
8. **The fan runs direct (no owner-confirm), bounded only by the structural caps**
   — consistent with jerv's existing direct web-class exec (the "chatbot feel").
   This makes decision-grade caps (#5, #6, the tree budget) *load-bearing*: they are
   the only bound, so they must be real harness enforcement, not prose.

## Why this fits (the lean litmus), and what it honestly costs

Per `ASSISTANT.md`'s litmus — reuse the adapter, storage, RLS-scoped Postgres, job
queue; add at most one small tool; stay operable by one person. It mostly fits, but
it is **not** as free as the first draft implied:

| Need | Reuse vs. net-new |
|---|---|
| Run a child turn | **Reuse** `AgentLoop.run` (`loop.py:293`) — takes `system`/`scopes`/`tools_allow`/`conversation`. |
| Mint child session + run-log row | **Reuse the building blocks** `AgentSessionRepo`, `AgentRunLog`, `agent_for` (as the Tasks runner does, `runner.py`) — but via a **new in-request spawn helper**, *not* the scheduler-invoked `TaskRunner` itself (which is neither concurrent nor awaited-in-request). |
| Personas | **Reuse** the `AgentProfile`/`AGENTS` shape (`agents.py:159`); add 3 profiles. `jerv` is the web-sandbox precedent. |
| The new tool | **Net-new** `.tool` sidecar + a spawn-service-backed handler. Plus a **registry "never-default" exclusion** so `curator.tools=None` does not absorb it. |
| **Live streaming of `subagent_*` to the chat** | **Net-new loop change.** Tool handlers return a `str` and have no event channel; the only live hatch is a fixed 4-tuple progress sink for *one* dispatching tool (`loop.py:430-548`). v1 must **generalize that into a `ChatEvent` queue the loop drains and forwards** (decision #2). |
| Parent⊆child clamp; depth; tree budget | **Net-new.** `Guardrails` is a frozen per-loop int (`loop.py:106`); `ToolContext` has no `depth` and no parent-tools concept. All of this is added machinery (see "Structural enforcement", "Tree budget"). |
| Lineage / run-log tree | **Net-new migrations** (see "Schema changes") — *not* "the only schema change", *not* "for free". |
| Web egress, bounded | **Reuse** jerv's `web_search` (SearXNG) + SSRF-guarded `web_fetch` (the SSRF guard re-applies per child automatically). |
| GUI glyphs / disclosure | **Reuse** `TurnGlyph`, `LiveToolStatus`, the OpsCard disclosure, `ProposalTree`. |

**Net-new, honestly:** one tool + registry exclusion; three personas + `.prompt`
files; the loop ChatEvent channel; the parent⊆child clamp + `depth` in
`ToolContext`; a mutable shared-budget object threaded through the loop's
accounting; a `no_memory` sandbox flag; several migrations; and the GUI surfaces.
Still zero new *runtime dependencies*, but a real loop/budget refactor.

## Architecture

### Execution model — in-request fan, and the live event channel

**Children are in-request `asyncio.gather` tasks the parent turn awaits — not the
scheduler's Tasks runner, and not detached background sessions.** The earlier draft
conflated three runtimes; this pins one. The `spawn_subagent` handler, while being
`await`ed by the parent loop (handlers are awaited one-at-a-time, `loop.py:549`),
launches `AgentLoop.run` for each child concurrently on the **same request event
loop** and `gather`s them. This is the only model under which (a) the parent can
collect summaries within the turn, (b) a parent cancel propagates `CancelledError`
into the children's `gather` (cancellation cascade works), and (c) the shared
budget counter can be an in-process integer.

**Live streaming requires a new loop event channel (the v1 cost of decision #2).**
Today a handler cannot push frames except the fixed `(step,total,preview,label)`
progress tuple, drained while one tool dispatches. v1 generalizes this: the spawn
handler is given an **event sink** that accepts arbitrary `subagent_*`
`ChatEvent`s; the parent's `run_stream` **drains that sink concurrently with the
awaited handler** and `yield`s the frames into the SSE stream. This is the
load-bearing backend change and is tracked as its own wave (below).

**`busy` / abort / reconnect contract (resolving the `activeTurn` lift):**

- The **parent turn stays the single gated turn.** `busy`/`abortRef`/`runIdRef`
  remain singletons keyed to the parent run; children are **sub-state of the parent
  turn**, not independently gated turns. Sends into the parent chat stay blocked
  while its fan runs (as today); a *sibling* chat is unaffected.
- **One keyspace per surface.** The in-chat accordion renders from the **parent
  turn's `subagent_*` events** (folded by `applyEvent` into the parent message),
  *not* from child `messagesBySession` buffers. The **session tree** renders from
  the child `AgentSession` rows plus a session-keyed live map fed by the same
  `subagent_*` events bubbled to a session-level store.
- **Reconnect replays the parent run.** A dropped socket resumes the *parent*
  `runId` (`/chat/runs/{id}/stream?after=N`); the parent re-emits buffered child
  progress. Clients never resume N child runs — consistent with fan-in A.
- `activeTurn` becomes a **session-keyed set** *only for the session-tree glyphs*
  (several rows animate at once); it does **not** gate sends. A richer per-row state
  enum (`spawning | researching | reviewing | summarizing | done | failed`) replaces
  the two-value `TurnKind` for these rows.

### The spawn primitive

A single new tool, `spawn_subagent`, in `jerv`'s allowlist (and, for nesting, the
research/review allowlists). It launches a **fan** in one atomic call:

```
tasks:        [{ persona, brief, label }]    # the fan: 1..N children
              #   persona ∈ research | review | summarize   (closed set)
              #   brief   = free-text at depth 0; {template_id, params} at depth ≥ 1 (decision #7)
              #   label   = short display name for the GUI row
max_parallel: integer                         # concurrency cap for this fan
scopes:       [domain]                         # optional; clamped ⊆ parent (jerv ⇒ empty)
```

One array call, not N separate calls (the live loop dispatches tool calls serially,
`loop.py:531`; an array launches the whole fan, which then runs concurrently). The
handler, per child:

1. **Validates `persona` against the closed spawn set `{research, review,
   summarize}` _before_ calling `agent_for`** — `agent_for` falls back to `curator`
   (KB-capable) on an unknown name (`agents.py:181`), so a malformed/injected
   persona must be rejected, never resolved.
2. **Clamps tools and scope to the parent** (decision #5): effective tools =
   `persona allowlist ∩ parent effective tools`, threaded into the child loop and
   enforced at `_dispatch`; effective scope = `requested ∩ parent scope`. With jerv
   as root the scope is empty all the way down.
3. **Checks the caps** (`parent.depth < 2`; fan-out; tree-size; budget admission).
   A refused spawn returns a structured `is_error` observation, never an exception.
4. **Mints a child `agent_session`** with `parent_session_id`, `depth =
   parent.depth + 1`, and the **`no_memory` sandbox flag** set; constructs the
   child `ToolContext` with `here = here_as_of = None` (no location inheritance,
   M2).
5. **Seeds the child conversation** with the brief as the **first user message
   inside the data/instruction boundary** (#1).
6. **Launches the child** as an in-request task and streams its `subagent_*` events
   up via the event sink.
7. The handler `gather`s the fan, **collects summaries in stable label order**, and
   returns them as tool-result data; the parent then synthesizes.

### The brief — insight without shared memory, and the depth≥1 lockdown

The "insight from the spawning session" is an explicit **brief** (data, not shared
memory or live parent access), wrapped in the data/instruction boundary (#1, #2).

- **At depth 0** (the owner's jerv turn) the brief may be **free-text** — it is
  composed in an owner-paced turn from owner-trusted context.
- **At depth ≥ 1** the brief is **template-bound** (decision #7): a child that has
  run `web_fetch` (untrusted content) may only spawn a grandchild with a
  `{template_id, params}` brief — structured fields filled into a fixed, versioned
  template, **never free-text prose**. This prevents an attacker-controlled fetched
  page from being laundered into a grandchild's steering instructions (the re-spawn
  hop the review flagged). The templates are the same three defaults below; only
  their parameter slots are model-filled at depth ≥ 1.

The persona **system prompt is never model-edited** (`.prompt` files are
version-pinned + CI-guarded); the model customizes the *brief*, not the prompt.
Defaults: **research** (search → corroborate → cited summary), **review**
(assess an artifact/claim, structured critique, no rewrite), **summarize**
(faithful structured condensation; no tools).

### Personas (web-sandboxed)

Three `AgentProfile`s added to `AGENTS`, shaped like `jerv`:
`reads_knowledge_base=False`, empty read scopes, **no KB tools**, `no_memory`.
Allowlists:

- **research / review**: `web_search`, `web_fetch`, `current_time`,
  `spawn_subagent`. **No `current_location`** (M2) even though the parent jerv holds
  it — the intersection drops it and the personas never list it.
- **summarize**: no tools; pure transform; cannot spawn.

**Correction (M2/m2):** jerv's surface is *not* "web only" — `JERV_TOOLS` also has
image/transcribe/video/server-metrics/`current_location`/weather. The clamp still
bounds children correctly (the personas don't list those), but the design must
*test* that a child's effective tools never include a non-web jerv tool, not assume
it from a mental model of "the jerv web surface".

### Structural enforcement (the safety properties, as code not prose)

Decision #8 makes these the *only* bound on a direct fan, so each must be harness
enforcement with a test that needs **zero model cooperation**:

- **Parent⊆child clamp:** pass `parent_effective_tools`/`parent_scopes` into the
  child `AgentLoop`; `allowed_names` intersects them; `_dispatch` refuses anything
  outside. Test: a child requesting a tool the parent lacked is refused *at
  dispatch*.
- **Depth cap:** add `depth` to `ToolContext`; spawn refused unless
  `parent.depth < 2`. Test: a depth-2 spawn is refused with no model cooperation.
- **Persona validation + curator-wildcard exclusion:** `spawn_subagent` is in a
  registry **never-default** set so `curator.tools=None` does not absorb it; persona
  is validated against the closed set before `agent_for`. Tests: `curator` is not
  offered `spawn_subagent`; an unknown persona never yields a KB agent.
- **No-memory:** the `no_memory` flag on the child session/loop **structurally
  disables episodic auto-append** (which is loop-driven, not a tool — so omitting
  `remember` from the allowlist is insufficient). Test: a child turn writes no
  `agent_episodes` row.
- **No location:** child `ToolContext.here`/`here_as_of` are `None`. Test: asserts
  so even when the parent turn carried a fix.

### Schema changes (a list, not "the only one")

Each new column/table ships the mandated RLS isolation test:

- `agent_sessions.parent_session_id UUID NULL REFERENCES agent_sessions(id)`,
  `agent_sessions.depth SMALLINT NOT NULL DEFAULT 0`, and a `no_memory`/sandbox
  marker (boolean). Roots default cleanly (`depth=0`, `parent=NULL`).
- `runs.parent_run_id` — **required** (not "optional") for the cost rollup, plus
  extend the `runs.kind` CHECK (currently `IN ('agent','integration','pipeline')`,
  migration 0037) to mark subagent runs (a new `kind` value or a discriminator
  column).
- **Extend the `agent` CHECK on both `agent_sessions` and `tasks`** (currently
  `IN ('curator','teacher','jerv','archivist')`, migrations 0070/0093/0095) to add
  `research`/`review`/`summarize`, or a child INSERT fails outright.

**RLS, right-sized (m3):** `agent_sessions` RLS is `is_owner()` only — rows are
owner-only *metadata*, not domain-scoped content. The new lineage columns inherit
that; there is no per-domain row firewall on the session table to test. The
firewall that matters is the **child's effective read scope** (the `owner_scoped`
GUC + `domain_scopes`), and under jerv-only-root that scope is **empty**, so the
"child can't read domain X" case is vacuously true. The meaningful test is the
*clamp* (structural enforcement, above), not RLS on the table. Don't oversell the
RLS surface.

### Guardrails & tree budget — a real loop change, on a corrected unit

The per-loop `Guardrails` is `@dataclass(frozen=True)` holding plain ints
(`loop.py:106`), and the loop's cost accounting **re-sums the whole growing message
list every ReAct step** (`cost += input+output`, `loop.py:327`) — so
`max_cost_tokens` already *over-counts* within a single loop. Two consequences the
budget design must honor:

1. **Redefine the accounting unit as _incremental_ spend** (delta input + output
   per model call) before any cross-tree pool math is meaningful. The strawman
   ceilings below are placeholders until this is fixed.
2. **The shared ceiling is a real mutable budget object** threaded through
   `AgentLoop.__init__`/`Guardrails` and checked at all four accounting sites
   (`run`, `run_stream`, `_run_stream_buffered`, `_produce_buffered`). It is not
   "reuse the existing accounting"; it is a budget-model change.

**One primary limiter + one floor** (the review showed three overlapping limiters
were contradictory):

- **Shared counter = the true ceiling.** The root turn owns a pool
  `tree_budget = base_max_cost × jerv.budget_multiplier × spawn_multiplier`. Every
  model call in the tree decrements it; a loop seeing it exhausted stops with
  `stop_reason="tree_budget_exhausted"`. Depth/fan-shape agnostic.
- **Root reserve** is carved off the top so the parent can always synthesize (and
  say "research truncated"): `children_pool = tree_budget − root_reserve`.
- **Admission floor:** a fan is admitted iff
  `remaining_children_pool ≥ n_children × min_viable_child_budget`.
- **`per_child_cap` is demoted to a sanity ceiling** (a child can't run away
  *individually*), not a reservation — the shared counter is the real bound.

**Reflexion in the tree (M6):** children run with **reflexion disabled** (and
`buffer_retry` forced off) — the parent's synthesis turn is the critique-worthy one;
per-child verify/retry would multiply uncosted model chains across the fan.

**Worked example (placeholders):** `spawn_multiplier=3` → `tree_budget≈2.4M`,
`root_reserve=400k`, `children_pool=2.0M`, `min_viable_child_budget=100k`,
`per_child_cap=600k` (sanity), `max_parallel=4`. A 4-child fan is admitted
(`2.0M ≥ 400k`), runs against the 2.0M pool, and stops fan-wide at exhaustion;
the root keeps its 400k to synthesize. Final numbers are a build-plan task.

### Fan ergonomics

- **Fan-out:** one array call (above).
- **Fan-in: model A (v1)** — the handler `gather`s the fan within the turn,
  streaming progress live, then folds summaries (data boundary) into a final
  synthesis step. **B (fire-and-continue)** and **C (streaming join)** are
  deferred; **B specifically must not auto-fire a synthesis turn on background
  completion** (that is the #10 untrusted-pacing trigger) — if built, re-entry is a
  fresh owner-visible turn.
- **Graceful degrade:** a child that errors/times out returns a **structured error
  summary**; the parent proceeds with the rest and the failure is surfaced (not
  swallowed) in the UI.
- **Cancellation cascades** (works because children are in-request `gather` tasks).
- **Stable label-order** collection for reproducible synthesis.
- **Budget gate:** an over-budget or over-cap fan is refused at spawn with an
  actionable observation.

### Security / non-negotiables it must respect

- **#1 boundary** — brief and summaries are data, never instruction, at every hop;
  the depth≥1 template-bound brief (decision #7) closes the re-spawn laundering hop.
- **#8 least privilege** — child tools/scope ⊆ parent, enforced at dispatch
  (structural, above), not handler discretion.
- **#9 egress** — web only, via jerv's SearXNG + SSRF-guarded `web_fetch` (the SSRF
  guard re-applies per child). Note the SSRF guard blocks *internal* targets, not
  *exfiltration to a public host*; the no-owner-data sandbox (no KB, no location,
  no memory) is what does the egress-safety work — so those invariants are
  load-bearing, not incidental.
- **#10 no untrusted trigger** — a spawn happens only inside an owner-initiated
  turn; fan-in B is deferred precisely to avoid a background-completion trigger.
- **#7/#11 memory & purge** — children are `no_memory` (structural); they touch no
  notes, so child runs carry no `note_id` refs and the deletion cascade is vacuous.
  Durable world-knowledge re-enters only through the notes door.

## GUI

**Mock gate cleared (owner-approved, incl. the non-happy states).** The revised
mocks add the **error, cancel, long-fan, and budget-exhausted** states (scenario
switchers) the first review missed, and the **persona-as-color scheme was rejected**
in favor of a neutral tag (below). The owner re-confirmed the revised mocks; the
layouts are settled (in-chat **A**, session-tree **B**).

### In-chat: live sub-agents — chosen **A, accordion step list** (revised)

- New `ChatEvent` variants `subagent_spawned`/`subagent_progress`/`subagent_done`
  folded by `applyEvent` (`transcript.ts`); rendered as a bordered collapsible step
  list in the parent bubble (the `ActivityLine`/`StepRow` register), reusing
  `LiveToolStatus` + `TurnGlyph`.
- **Persona is a neutral text tag (or a per-persona glyph on a neutral disc), never
  a color** — the binding rule is "components express `kind` enums, never colors",
  and the rejected scheme collided with green=live/ok, violet=finance,
  steel=agent/live-glyph (review F1). Color stays semantic: steel=live, green=done,
  **rose=failed**.
- **Required states:** a **failed child** (rose `✕`, error phase word, row
  auto-expands its error like `StepRow`); the header rolls up `done · 3 ran · 1
  failed`; a **Stop** on the fan header (cascade cancel, mirroring the image-render
  Stop), and optional per-child cancel; the **budget meter** goes danger at the
  ceiling with a paired text value, and a **truncated** synthesis variant when
  `tree_budget_exhausted`; a designed appearance for a **refused/over-cap spawn**
  observation.
- **Long-fan containment:** a row cap + "show N more" and a max-height scroll region
  so a 16-leaf fan doesn't turn the bubble into a wall.
- **Accessibility:** glyph `aria-hidden` with the status word carrying state; **one
  polite live-region summary** for the whole fan ("3 researchers running… →
  synthesized from 3"), not N live rows (avoids the announcement storm);
  `prefers-reduced-motion` disables the bounce.
- The fan-out result is a **registered `subagent_synthesis` tool-view** — it must be
  added to the `DESIGN.md` registry list in the same PR (it is not bespoke markup),
  composed from `stat_block`/`citation_card` primitives, with the standard
  tool-view frame (no bespoke green panel).

### Session manager: nested sub-agents — chosen **B, always-nested rail** (revised)

- `AgentSession` payload gains `parent_session_id?` / `subagent_count?`.
  **Children are excluded from top-level bucketing** (filtered by
  `parent_session_id != null`) — they appear only nested, never as their own
  top-level rows.
- Rendered as a vertical connector rail under the parent; **the group collapses by
  default once `subagent_count` exceeds a threshold** (so a big fan doesn't bury the
  rest of the picker the Chats redesign just made dense). An **archived** parent's
  rail is collapsed/count-only.
- Child rows reuse the live-turn glyph + neutral persona tag + status (incl.
  **failed**); the parent badge distinguishes running / `done · N ran` / `… · 1
  failed`. Tree semantics for a11y: `role="tree"/"treeitem"`, `aria-level`,
  `aria-expanded`, and the group toggle is a **button** (not a `div`).
- `activeTurn` becomes a session-keyed **set for the row glyphs only** (it does not
  gate sends — see "Execution model"). This is the sanctioned lift of the
  "at most one chat shows the live glyph" rule.

## Testing

- **Structural caps (no model cooperation):** depth-2 spawn refused; over-budget /
  over-cap fan refused; child tool outside `persona ∩ parent` refused at dispatch;
  `curator` not offered `spawn_subagent`; unknown persona never resolves to a KB
  agent.
- **Sandbox invariants:** child writes no `agent_episodes` row; child
  `ToolContext.here is None`; child effective tools include no non-web jerv tool.
- **Budget:** incremental-spend accounting; shared-counter depletion, exhaustion
  `stop_reason`, root_reserve survival, admission-floor refusal — all
  deterministic with the adapter fake.
- **Boundary:** a depth≥1 free-text brief is rejected (template-bound only).
- **Migrations/RLS:** isolation tests for the new columns (owner-only); CHECK
  extensions accept the three personas.
- **Frontend:** reducer tests for `subagent_*`; mock fixtures default/empty/long/
  **error**/offline/**budget-exhausted**.
- 80% backend coverage, security paths 100%, real Postgres via testcontainers, LLM
  faked (`CLAUDE.md` #5).

## Wave split

Per `docs/PROCESS.md`: each wave runs its tasks in parallel worktrees off a
`wave-Sn` integration branch, gets an independent **per-task** adversarial review
(a *different* agent than the builder), then a **wave-level** adversarial review of
the whole wave diff (security/red-team for any RLS/firewall/scope/data-boundary
surface), and lands as **exactly one PR**, CI green before merge. The two GUI waves
(S3, S4) go through the **mock gate**; both layouts are already chosen and
owner-approved (in-chat **A**, session-tree **B** — `DESIGN.md` §"Sub-agent
spawning surfaces", mocks `docs/mocks/subagent-{chat,sessions}-mock.html`), so the
only remaining gate is the **non-happy-state mock re-review before S3** (M7).

Every wave carries the `CLAUDE.md` #5 test bar: **80% backend coverage, security
paths 100%, real Postgres via testcontainers, all LLM calls faked.** The structural
caps (depth/fan/clamp/no-memory/location) get tests that need **zero model
cooperation** (decision #8 makes them the only bound on a direct fan). Each new
table/column ships its mandated RLS isolation test. Per-wave adversarial reviews
**re-check the `SUBAGENT_SPAWNING_REVIEW.md` findings** on the surfaces that wave
touches (cited per wave).

The four design phases map 1:1 to waves **S1–S4**. S2 depends on S1 (the fan
helper); S3 depends on S2 (the ChatEvent channel + budget events it renders); S4
depends on S1 (lineage columns) and can overlap S3 (a different surface).

### Wave S1 — Spawn core + structural enforcement *(backend; security/red-team gated)*

Phase 1. The tool, the personas, the clamp, the caps, the migrations, the
in-request fan helper. **Children run end-to-end but are visible only in the Ops
run-log — no chat UI, no live streaming, no shared budget yet** (those are S2/S3).
Re-checks review findings **B2, B3, M1, M2, M3, M4, m2, m3, m4**.

- **S1.1 — Schema & migrations (+ RLS isolation tests).** `agent_sessions`:
  `parent_session_id UUID NULL REFERENCES agent_sessions(id)`, `depth SMALLINT NOT
  NULL DEFAULT 0`, `no_memory BOOLEAN NOT NULL DEFAULT false` (roots default
  cleanly). `runs`: `parent_run_id` (**required** for the cost rollup) + the `kind`
  discriminator for subagent runs. Extend the `agent` CHECK on **both**
  `agent_sessions` and `tasks` to add `research`/`review`/`summarize` (else a child
  INSERT fails). **Wave decision — `runs` discriminator:** add a new
  **`kind='subagent'`** value (extending the `IN ('agent','integration','pipeline')`
  CHECK from migration 0037) rather than a marker column — it keeps run-log filters
  one-dimensional and matches the existing `kind` pattern; *recommend, finalize in
  this task.* **Wave decision — child transcript persistence:** persist the
  **run-log row + the returned summary only** in v1; defer full re-openable child
  transcripts to a follow-on (keeps storage lean; lineage + summary cover audit).
  Tests: RLS isolation per new column (`agent_sessions` is `is_owner()`-only —
  owner-only metadata, right-sized per m3); CHECK accepts the three personas; root
  rows default `depth=0`/`parent=NULL`.
- **S1.2 — Personas + `.prompt` files + brief templates.** Three `AgentProfile`s
  added to `AGENTS`, shaped like `jerv`: `reads_knowledge_base=False`, empty read
  scopes, **no KB tools**, `no_memory`. Allowlists: research/review =
  `web_search`, `web_fetch`, `current_time`, `spawn_subagent` (**no
  `current_location`**, M2); summarize = no tools (pure transform, cannot spawn).
  Version-pinned + CI-guarded `.prompt` files (system prompt never model-edited).
  The three default **brief templates** (research/review/summarize) with the
  parameter slots model-filled at depth ≥ 1 (decision #7). Extend the `agent` CHECK
  fixtures alongside S1.1.
- **S1.3 — Spawn handler, structural enforcement & in-request fan.** The
  `spawn_subagent` `.tool` sidecar + handler; the registry **never-default
  exclusion** so `curator.tools=None` does not absorb it; **persona validated
  against the closed `{research,review,summarize}` set before `agent_for`** (which
  falls back to KB-capable `curator` on unknown, `agents.py:181`). The
  **parent⊆child clamp**: `parent_effective_tools`/`parent_scopes` threaded into the
  child `AgentLoop`, intersected, refused at `_dispatch`. `depth` added to
  `ToolContext`; spawn refused unless `parent.depth < 2`. Structural **`no_memory`**
  (disables the loop-driven episodic auto-append, not just an allowlist omission);
  child `ToolContext.here = here_as_of = None` (M2). The **in-request gather fan
  helper** that mints child `agent_session` + run-log rows via the building blocks
  (`AgentSessionRepo`, `AgentRunLog`, `agent_for` — *not* the scheduler's
  `TaskRunner`), seeds the brief inside the data/instruction boundary, runs the fan
  with `asyncio.gather`, collects summaries in **stable label order**, and
  degrades gracefully (a child error → structured error summary, surfaced not
  swallowed; cancellation cascades). The **static** caps land here as harness
  enforcement: `parent.depth < 2`, `max_children_per_parent`, `max_parallel`,
  `max_total_agents_per_tree` (the *budget* admission floor is S2). **Structural
  tests (no model cooperation):** depth-2 spawn refused; child tool outside
  `persona ∩ parent` refused at dispatch; `curator` not offered `spawn_subagent`;
  unknown persona never resolves to a KB agent; over-fan / over-tree-size refused;
  child writes **no** `agent_episodes` row; child `ToolContext.here is None`; child
  effective tools include **no non-web jerv tool** (m2); depth≥1 free-text brief
  rejected (template-bound only). Depends on S1.1 + S1.2.

**S1 wave-level review (security/red-team):** confirm the clamp is real at
`_dispatch` (B2), persona validation + curator exclusion (B3), `no_memory`
structural not persona-trusted (M3), location nulling (M2), the direct fan is
genuinely bounded only by *enforced* caps (M4), no non-web jerv tool leaks (m2).

### Wave S2 — Loop ChatEvent channel + tree budget *(backend; security/red-team gated; budget-value escalation)*

Phase 2. The load-bearing loop refactor: a generalized event channel so children
can stream, and the corrected shared-budget model. Still no chat *rendering* (S3)
— this wave makes the backend emit `subagent_*` events and enforce the tree
budget. Re-checks **B1, M5, M6, M8**.

- **S2.1 — Loop ChatEvent channel.** Generalize the fixed
  `(step,total,preview,label)` progress sink (`loop.py:430-548`) into a
  **`ChatEvent` queue the loop drains concurrently with the awaited handler** and
  `yield`s into the SSE stream; the spawn handler is handed an **event sink**
  accepting arbitrary `subagent_*` `ChatEvent`s. Buffer frames so a **reconnect
  replays the parent run** (`/chat/runs/{id}/stream?after=N`) — clients never
  resume N child runs (B1, M8). The parent turn stays the **single gated turn**
  (`busy`/`abortRef`/`runIdRef` singletons unchanged). Tests: events drain
  concurrently with an awaited handler; reconnect replays buffered `subagent_*`
  frames from an offset; a sibling chat is unaffected while the fan runs.
- **S2.2 — Incremental-spend accounting.** Redefine the loop's cost unit as
  **incremental spend** (delta input+output per model call) at all four accounting
  sites (`run`, `run_stream`, `_run_stream_buffered`, `_produce_buffered`), fixing
  the per-step re-sum over-count (`loop.py:327`). Prerequisite for any cross-tree
  pool math (M5). Tests: incremental accounting is exact and monotone with the
  adapter fake; a single loop no longer over-counts.
- **S2.3 — Shared tree budget.** A **mutable shared-budget object** threaded
  through `AgentLoop.__init__`/`Guardrails`, checked at the four sites:
  `tree_budget = base_max_cost × jerv.budget_multiplier × spawn_multiplier`; a
  **root reserve** carved off the top (`children_pool = tree_budget − root_reserve`)
  so the parent can always synthesize and say "research truncated"; the
  **admission floor** (`remaining_children_pool ≥ n_children ×
  min_viable_child_budget`); `per_child_cap` demoted to a **sanity ceiling**; a loop
  seeing the pool exhausted stops with `stop_reason="tree_budget_exhausted"`.
  Children run with **reflexion disabled** and `buffer_retry` forced off (M6).
  **Wave decision (budget values — RETUNED on-box, post-merge).** The first on-box run
  exposed the real failure mode: on a single-GPU box (gpt-oss-120b at ~5 tok/s) a child
  ground to its *token* budget — 31–49 steps, ~410k tokens, ~11 min each — while the UI
  showed no movement, and the client turn errored before the fan finished. The fix
  separates **runtime** bounds from the **token** ceiling:
  - **Tree ceiling raised to `spawn_multiplier = 2.5`** (~2.0M with jerv's 800k root
    cap; `root_reserve` 25% = 500k, `children_pool` = 1.5M) — generous headroom so
    *budget exhaustion is the backstop, not the stopper*.
  - **Per-child RUNTIME caps are the real bound:** `CHILD_MAX_STEPS = 10` +
    `CHILD_WALL_CLOCK_S = 180` (a hard `asyncio.wait_for`; a child past it returns a
    `timeout` degraded result so one slow child can't stall the fan), with
    `CHILD_MAX_COST_TOKENS = 400k` as a backstop that should rarely bite. Each child
    now finishes in ~2–3 min; the wall-clock bounds the whole (parallel) fan to ~one
    child's time, so **serial was not needed** (it would only have doubled wall-clock).
  - **Live per-step progress:** `AgentLoop.run` gained an `on_step` hook; the spawn
    service emits a `subagent_progress` every child step carrying the step count and the
    live `tree_spent`, so the in-chat budget meter and per-row step count move while a
    (non-streaming) child works — the gap that made a working fan look frozen.

  `max_parallel=4`, `max_children_per_parent=6`, `max_total_agents_per_tree=12`,
  `min_viable_child_budget=100k` unchanged. Tests: shared-counter depletion, exhaustion
  `stop_reason`, root_reserve survival, admission-floor refusal, per-step progress
  emission, and the wall-clock degrade — all deterministic with the adapter fake;
  reflexion/`buffer_retry` proven off for children.

**S2 wave-level review (architecture/runtime):** the streaming path is real and the
reconnect replays the *parent* run not N children (B1, M8); the budget is on an
incremental unit with a single load-bearing limiter + floor, not three
contradictory ones (M5); reflexion is off in the tree (M6); the parent stays the
single gated turn.

**Resolved in the S2 review (two design clarifications the spec did not pin):**
- **M6 at the parent layer:** `buffer_retry` reflexion re-produces a turn, which would
  re-dispatch `spawn_subagent` and re-run the *entire fan* (new child sessions + spend)
  per retry — the exact "multiply model chains across the fan" failure M6 names. So a
  **spawner agent's turn forces `buffer_retry` off** (post-hoc verify-and-annotate still
  applies). A spawner therefore always streams its fan live (no buffered-path gap).
- **Root reserve is best-effort, the total is hard.** Loops charge after each model call,
  so a fan of up to `max_parallel` children can have that many calls in flight when the
  children's pool is crossed — `spent` overshoots the children's pool by a bounded batch,
  *eroding* (not breaching) the reserve. What is hard: total tree spend is bounded (no
  runaway) and the root **always completes its synthesis call** (the budget check is
  post-call). A budget-cut child returns its partial answer tagged `[truncated]`.

### Wave S3 — Live chat surface + `subagent_synthesis` tool-view *(GUI; mock re-review gate)*

Phase 3. Render the fan in the parent bubble. **Mock gate:** layout **A** is chosen
and owner-approved; the remaining gate is a **re-review sign-off on the non-happy
states** (failed / cancel / budget-exhausted-truncated / long-fan) before
implementation begins (M7, open item). Re-checks **B4, M7, M10, m5, m6**.

- **S3.1 — `subagent_*` reducer + in-chat accordion (variant A).** New `ChatEvent`
  variants `subagent_spawned`/`subagent_progress`/`subagent_done` folded by
  `applyEvent` (`transcript.ts`), rendered as a bordered collapsible step list in
  the parent bubble (the `ActivityLine`/`StepRow` register, reusing
  `LiveToolStatus` + `TurnGlyph`). **Persona = a neutral text tag, never a color**
  (B4); semantic color stays `steel=live / green=done / rose=failed`. Required
  states: a failed child (rose `✕`, error phase word, row auto-expands its error);
  header roll-up `done · N ran · M failed`; a **Stop** on the fan header (cascade
  cancel) + optional per-child cancel; the **budget meter** to `--danger` at the
  ceiling with a paired text value and a **truncated** synthesis variant on
  `tree_budget_exhausted`; a designed **refused/over-cap spawn** observation.
  **Long-fan containment** (row cap + "show N more" + max-height scroll, M10).
  **Accessibility:** `aria-hidden` glyph with the status word carrying state; **one
  polite live-region summary** for the whole fan (not N rows); `prefers-reduced-
  motion` disables the bounce. Tests: reducer tests for `subagent_*`; mock fixtures
  default / empty / long / **error** / offline / **budget-exhausted**.
- **S3.2 — `subagent_synthesis` tool-view + registry.** The fan-out result is a
  **registered tool-view** composed from `stat_block`/`citation_card` primitives in
  the standard tool-view frame (no bespoke green panel); **added to the `DESIGN.md`
  registry list in the same PR** (the same-PR rule, m5).

**S3 wave-level review (GUI/design-system):** persona is a neutral tag not a color
(B4); every non-happy state is present and designed (M7); long fans are contained
(M10); the synthesis card is registered, not bespoke (m5); token-bound classes,
reduced-motion, `aria-hidden` glyphs, polite live region (m6).

### Wave S4 — Session-tree surface *(GUI; mock gate already cleared — layout B)*

Phase 4. Show the lineage in the session manager. Layout **B** (always-nested rail)
is chosen and owner-approved; no further mock round needed. Re-checks **M8, M10,
m6**. Can overlap S3 (different surface, shares only the lineage columns from S1).

- **S4.1 — Payload + nested rail.** `AgentSession` payload gains
  `parent_session_id?` / `subagent_count?`; **children excluded from top-level
  bucketing** (filtered by `parent_session_id != null`) — nested only, never their
  own top-level rows (M10). Rendered as a vertical connector rail under the parent;
  the group **collapses by default once `subagent_count` exceeds a threshold** (and
  for any archived parent). Child rows reuse the live-turn glyph + neutral persona
  tag + status (incl. **failed**); the parent badge distinguishes `N running` /
  `done · N ran` / `… · M failed`. Tree a11y: `role="tree"/"treeitem"`,
  `aria-level`, `aria-expanded`; the group toggle is a real **`button`** (m6).
- **S4.2 — `activeTurn` session-keyed set (row glyphs only).** `activeTurn` becomes
  a session-keyed **set** that drives **row glyphs only — it does not gate sends**
  (the parent turn stays the single gated turn, M8); a richer per-row state enum
  (`spawning | researching | reviewing | summarizing | done | failed`) replaces the
  two-value `TurnKind` for these rows. This is the sanctioned lift of the "at most
  one chat shows the live glyph" rule.

**S4 wave-level review (GUI + correctness):** the `activeTurn`-set drives glyphs
only and does **not** regress the single-gated-turn invariant (M8); children stay
out of top-level bucketing and the archived rail is collapsed (M10); tree roles and
the real-`button` toggle (m6).

## Deferred past v1 (not waves)

*All v1 decisions are settled above: execution model (in-request fan-in A),
live-in-v1, web-sandbox, jerv-root, child⊆parent, depth `< 2`, template-bound
depth≥1 briefs, direct caps-bounded fan, the tree budget (single shared counter +
root reserve + admission floor on an incremental unit), reflexion-off for children,
both GUI layouts, the `runs` discriminator (S1.1), and child-transcript persistence
(summary+run-log, S1.1).*

- **Fan-in B/C** — only if model A proves insufficient. **B (fire-and-continue)**
  must **not** auto-fire a synthesis turn on background completion (the #10
  untrusted-pacing trigger); if built, re-entry is a **fresh owner-visible turn**.
- **Full child transcript persistence** — re-openable child transcripts (beyond the
  run-log + summary), if the audit/re-open need materializes.
