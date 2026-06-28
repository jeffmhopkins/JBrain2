# Sub-agent spawning — design spec (proposed)

**Status: proposed, not scheduled.** This is the icebox design for letting the
web-sandboxed agent (`jerv`) spawn web-sandboxed **research / review / summarize**
sub-agents for context flexibility. Nothing here is built. When picked up it must
be reconciled with the `CLAUDE.md` non-negotiables and the `docs/ASSISTANT.md`
agent design, given a roadmap slot, and promoted out of `proposed/`.

> **Read this first — honesty about what exists.** An adversarial review (security
> + architecture + GUI) found the earlier draft asserted properties as
> *"settled / structural / harness-enforced / reuses-existing"* that the live code
> does **not** enforce today. This revision corrects that: every safety property
> below (the parent⊆child clamp, the depth/fan/budget caps, the no-memory and
> no-location guarantees) is **net-new machinery that the build must add to the
> loop/registry/schema** — it is *not* present in `loop.py` today. Where the design
> needs a loop or schema change, this doc says so. The structural enforcement
> *is the work*; until it ships, none of these guarantees hold.

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

## Proposed phasing (when scheduled)

1. **Spawn core + structural enforcement** — the tool + registry exclusion, the 3
   personas + `.prompt` files, the parent⊆child clamp + `depth` in `ToolContext`,
   persona validation, the `no_memory` flag + location nulling, the migrations, and
   the depth/fan caps. Children run via the in-request gather helper; **visible only
   in the Ops run-log** (no chat UI yet).
2. **Loop event channel + tree budget** — generalize the progress sink into a
   `ChatEvent` queue the loop drains; the incremental-spend accounting + shared
   counter + root reserve + admission gate.
3. **Live chat surface** — `subagent_*` events, the in-chat accordion + the
   `subagent_synthesis` tool-view, the failure/cancel/exhausted/long-fan states
   (after the provisional mock re-review).
4. **Session-tree surface** — `parent_session_id` in the payload, the nested rail
   with children excluded from bucketing, the `activeTurn`-set for row glyphs.

## Open questions for the build plan

*Settled above: execution model (in-request fan-in A), live-in-v1, web-sandbox,
jerv-root, child⊆parent, depth `< 2`, template-bound depth≥1 briefs, direct
caps-bounded fan, tree budget (single shared counter + root reserve + admission
floor, on an incremental unit), reflexion-off for children.*

- **Concrete cap numbers** — `spawn_multiplier`, `root_reserve`,
  `min_viable_child_budget`, `per_child_cap` sanity ceiling, `max_children_per_
  parent`, `max_total_agents_per_tree`, `max_parallel`, per-child wall-clock — tuned
  against a real research sweep.
- **`runs` discriminator** — new `kind='subagent'` value vs. a marker column +
  keep `kind='agent'`.
- **Child transcript persistence** — full child transcripts (audit / re-open) vs.
  summary + run-log only.
- **Mock re-review** — sign off the failure/cancel/exhausted/long-fan states before
  the Phase-3 build.
- **Fan-in B/C** — only if model A proves insufficient; B's re-entry must be a
  fresh owner-visible turn.
