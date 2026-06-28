# Sub-agent spawning — design spec (proposed)

**Status: proposed, not scheduled.** This is the icebox design for letting the
Full Brain agent spawn web-sandboxed **research / review / summarize** sub-agents
for context flexibility. Nothing here is built. When picked up it must be
reconciled with the `CLAUDE.md` non-negotiables and the `docs/ASSISTANT.md`
agent design, given a roadmap slot, and promoted out of `proposed/`.

It builds directly on the single hatch `docs/ASSISTANT.md` already reserves:

> **No standing multi-agent orchestra.** One context window holds a personal chat
> task. Keep exactly one narrow `spawn_subagent` escape hatch for rare fan-out
> (wide retrieval sweeps): it runs the **same loop with a fresh context and the
> same RLS-scoped tool set, returns only a summary** — it is context isolation,
> **not** a code-execution or privilege-escalation path. (`ASSISTANT.md:170`)

This spec expands that hatch into a small, bounded fan-out tree: typed sub-agent
personas, a parent-authored brief, live progress surfaced in the chat and the
session manager, and nesting capped at 3 layers — while keeping the load-bearing
property intact (**context isolation, never privilege escalation**).

## The idea in one paragraph

The web-sandboxed agent (`jerv`) reads a default **brief template** (research /
review / summarize), tweaks it for the task at hand, and calls `spawn_subagent` to
launch a **fan** of one or more child sessions. Each child runs the **same
`AgentLoop`** with a web-sandboxed persona (no knowledge-base access), **tools and
access that can only narrow** relative to the parent, its **own guardrail budget
drawn from a shared tree budget**, and a **depth counter** that refuses to spawn
past **two sub-agent layers (three including the main session)**. Children run
**detached and concurrent** (the Tasks-runner pattern), stream live progress up to
the parent's chat and the session tree, and return **only a summary** as data. The
parent reads the summaries, cites them, and composes the final answer.

## Settled decisions (owner, 2026-06)

1. **Execution model: detached + live.** Children run as background sessions and
   stream live progress into the chat and the session tree; the parent collects
   summaries when they finish. (Not the simpler blocking-inline variant.)
2. **Sub-agent access: web-sandboxed (jerv-style).** Research/review/summarize
   personas read **no knowledge base** — web tools + transform only, returning
   cited summaries. This is the cheapest, safest firewall story: almost no
   personal context can ride along off-box.
3. **Nesting depth: 3 layers including the main session** — i.e. **two layers of
   sub-agents** (root → sub-agent → sub-sub-agent). The root is depth 0; a session
   at depth 2 is a leaf and **cannot** spawn. Enforced structurally by the harness.
4. **The spawner is `jerv`,** not `curator`. This is what makes tool/access
   inheritance (below) conflict-free: `curator` holds no web tools (web is
   opt-in, jerv-only), so a web-research child could never inherit web access from
   it. The web-sandboxed `jerv` is the natural — and only coherent — root.
5. **Children never exceed the parent.** A child's tools and access are always
   **⊆ its parent's** (intersected with the child persona's own allowlist). A
   child can never gain a tool or a scope the parent lacked.

## Why this fits (the lean litmus)

Per `ASSISTANT.md`'s litmus — reuse the LLM adapter, storage abstraction,
RLS-scoped Postgres, and the job queue; add at most one small, well-shaped tool;
keep it operable by one person — this is a strong fit. Almost everything already
exists:

| Need | Reuses (concrete) |
|---|---|
| Run the child | `AgentLoop.run` / `run_stream` (`backend/src/jbrain/agent/loop.py:293`) — already takes `system`, `scopes`, `tools_allow`, `conversation` |
| Mint a fresh session + run headless + record a run | The Tasks runner `LoopTurnExecutor` (`backend/src/jbrain/tasks/runner.py:58`) — the proven "new `agent_session` → loop → run-log" template |
| Personas (system prompt + tool allowlist + KB flag + budget) | `AgentProfile` / `AGENTS` (`backend/src/jbrain/agent/agents.py:112,159`); `jerv` is the web-sandbox precedent (`reads_knowledge_base=False`, `budget_multiplier=4`) |
| The new tool | One `.tool` sidecar + handler (`agent/toolregistry.py`, dispatch-time allowlist at `loop.py:899`) |
| Audit / cost / progress | The unified run-log `app.runs` / `run_steps` (`backend/src/jbrain/models/agent.py:51`) |
| Web egress, bounded | jerv's existing `web_search` (self-hosted SearXNG) + SSRF-guarded `web_fetch` |
| Live streaming to the phone | The `ChatEvent` union + SSE path + detached-turn broker (`api/agent.py`, `frontend/src/agent/transcript.ts`) |
| Live session glyphs / collapsible groups | `TurnGlyph` (`SessionsPanel.tsx:622`), `LiveToolStatus`, the OpsCard disclosure pattern, `ProposalTree` (tree w/ per-node status) |

**Net-new is small:** one tool, three personas + their `.prompt` files, a
parent/child linkage column + depth/budget plumbing, a few new `ChatEvent`
variants, and the GUI surfaces. **Goal: zero new runtime dependencies.**

## Architecture

### The spawn primitive

A single new tool, `spawn_subagent`, in **`jerv`'s** allowlist (and, for nesting,
the research/review personas' allowlists too — gated by **depth**, not persona).
`curator` is deliberately **not** a spawner (settled decision #4). The tool
launches a **fan** of one or more children in a single atomic call:

```
tasks:        [{ persona, brief, label }]    # the fan: 1..N children
              #   persona ∈ research | review | summarize   (closed set, code-defined)
              #   brief   = task + curated context           (data, not instruction)
              #   label   = short display name for the GUI row
max_parallel: integer                         # concurrency cap for this fan
scopes:       [domain]                         # optional; clamped ⊆ parent (jerv ⇒ empty)
```

A **single array call**, not N separate tool calls — see "Fan ergonomics" for
why. The handler, per child:

1. **Resolves the child persona** via `agent_for(persona)` — fixed, versioned
   system prompt + tool allowlist + `reads_knowledge_base=False`.
2. **Clamps tools and scope to the parent** (settled #5): effective tools =
   `persona allowlist ∩ parent's effective tools`; effective scope =
   `requested ∩ parent scope` (narrow-only — non-negotiable #8). With `jerv` as
   root the scope is empty all the way down (no KB anywhere in the tree).
3. **Checks the harness caps** (depth ≤ 2, fan-out, tree budget — below). A spawn
   from a depth-2 session, or one over a cap, returns a structured `is_error`
   observation the model can react to, never an exception.
4. **Mints a child `agent_session`** with `parent_session_id` set and `depth =
   parent.depth + 1`, reusing `AgentSessionRepo` (the Tasks-runner path).
5. **Seeds the child conversation** with the brief as the **first user message
   inside the data/instruction boundary** (non-negotiable #1) — see "The brief".
6. **Launches the child detached** (background run, like a Task), concurrently
   across the fan, and returns the **handles** inline (child `session_id`/
   `run_id`s), not a blocking result.
7. **Streams each child's progress** up as new `ChatEvent`s; the parent
   **collects** the summaries per "Fan ergonomics" below.

A single call fans out several children; each is an independent detached run.
How the parent **collects** their summaries and continues is the "Fan ergonomics"
section below.

### The brief — "insight from the spawning session" without shared memory

The "component that gives the sub-agent insight from the spawning session" is an
explicit **brief**, not shared memory or live parent access. The parent composes
it (task + a curated context block) and it becomes the child's first user
message, **wrapped in the data/instruction boundary** — so the child reads it as
*"here is the task and what's known,"* never as executable instruction
(non-negotiables #1, #2). This preserves context isolation: the child gets what
the parent chose to hand down, as data, and nothing more.

**Default brief templates** are readable, model-customizable starting points
(`list_subagent_templates` → read → tweak → pass as `brief`). Critically, the
**system prompt is never model-edited** — `.prompt`/`.tool` files are
version-pinned and CI-guarded (a prose/param change without a version bump fails
the build). So "jerv can read and modify the default prompt before calling" means
the model customizes the **brief / task parameter** (a user-channel string), not
the persona's system prompt. The three defaults:

- **research** — "Investigate `<topic>`. Search the open web, corroborate across
  independent sources, and return a cited summary of findings, key claims, and
  open questions. Prefer primary sources; flag uncertainty." (web tools)
- **review** — "Critically assess `<artifact/claim>`. Identify errors, weak
  evidence, missing considerations, and counter-arguments. Return a structured
  critique with severity. Do not rewrite — assess." (web tools optional, for
  fact-checking)
- **summarize** — "Condense `<inputs>` into a faithful, structured summary at
  `<length>`. Preserve nuance and attribution; drop nothing load-bearing." (no
  tools; pure transform)

### Personas (web-sandboxed)

Three new `AgentProfile`s added to the closed `AGENTS` set, each shaped like
`jerv` (`agents.py`): `reads_knowledge_base=False`, empty default read scopes,
**no KB tools**, writes no episodic memory. Allowlists:

- **research**: `web_search`, `web_fetch`, `current_time`, **`spawn_subagent`**
  (the bounded jerv web surface — SearXNG + SSRF-guarded fetch — plus the ability
  to fan out one more layer).
- **review**: same web surface + `spawn_subagent`; no mutate/KB tools.
- **summarize**: no tools (like `teacher`); pure transform, cannot spawn.

research/review carry `spawn_subagent` so the tree can nest, but the **depth cap
does the gating, not the persona**: a depth-2 (sub-sub-agent) call to spawn is
refused structurally regardless of allowlist. Per settled #5 a child's tools are
also clamped to the parent's, so a sub-agent never holds a tool jerv lacked.

### Lineage (the only schema change)

A nullable self-FK records the tree:

- `agent_sessions.parent_session_id UUID NULL REFERENCES agent_sessions(id)` and
  `agent_sessions.depth SMALLINT NOT NULL DEFAULT 0` (migration). Root chats are
  `depth=0, parent=NULL`; existing rows default cleanly.
- Optionally mirror `parent_run_id` on `app.runs` for the Ops run-log tree.

Sub-agent sessions remain **owner-only metadata** under the same RLS policy as
`agent_sessions` today. **Each new column ships the mandated RLS isolation test**,
including one proving a child session cannot read a domain outside its
(narrowed) scope and that a single-scope viewer cannot read a multi-scope
episode in the tree.

### Guardrails — structural, never prompt-trusted

Nesting × fan-out is the #1 risk — even at two sub-agent layers, a 4-wide fan is
`1 + 4 + 16 = 21` agents. All caps are enforced by the harness at spawn time,
alongside the existing per-loop `Guardrails` (`loop.py:106`):

- **`max_depth = 2`** (root is depth 0 → 3 layers including the main session) —
  the child's `depth` rides its `ToolContext`; a spawn from a depth-2 session is
  refused structurally (same enforcement class as the dispatch-time tool
  allowlist), never a prompt suggestion.
- **`max_children_per_parent`** and **`max_total_agents_per_tree`** — fan-out and
  tree-size caps; over-cap spawns refused with an actionable observation.
- **Shared tree token budget.** One hard ceiling for the whole subtree, charged
  to the root — see "Tree budget" below.
- **Wall-clock + cancellation.** A parent `cancel` cascades to its subtree (the
  detached children are cancelable like any run; `POST /chat/runs/{id}/cancel`).

### Tree budget — one ceiling, charged to the root

Don't invent a free-floating budget concept: reuse the existing per-turn
`max_cost_tokens` guardrail as the **single hard ceiling for the entire subtree**
— root synthesis plus every descendant's model calls all charge against it. This
keeps `ASSISTANT.md`'s rule intact ("a live interactive turn must never be starved
by a background job"), because the tree can't outspend a number the harness
already governs. A fan-out turn *is* a bigger unit of work than a chat turn, so
the spawn-capable turn's ceiling is `base_max_cost × jerv.budget_multiplier ×
spawn_multiplier` — one new small, configurable `spawn_multiplier`, still a fixed
hard cap.

Three enforcement points, all reusing the loop's existing usage accounting (which
already sums against `max_cost_tokens`):

1. **Shared atomic counter = the true ceiling.** Every model call anywhere in the
   tree decrements one root-owned pool; any loop whose next accounting check sees
   it exhausted stops fail-closed with `stop_reason="tree_budget_exhausted"`. This
   is the only thing that truly bounds total cost, and it is **depth/fan-shape
   agnostic** — a grandchild and a child draw from the same integer, so nesting
   needs no per-level math.
2. **Per-child reservation = anti-starvation.** Each child's own
   `Guardrails.max_cost_tokens` is set to a carved slice (`per_child_cap`) at
   spawn, so one greedy or runaway child can't drain the pool before its siblings
   run. A child stops at the **smaller** of (its slice, the shared pool).
3. **Root reserve = protect the live answer.** Carve a `root_reserve` off the top
   **before** fanning out, so after the children spend, the root still has budget
   to synthesize the final answer (and to note "research was truncated" if a child
   ran out). So `tree_budget = root_reserve + children_pool`; children only ever
   draw from `children_pool`.

**Admission gate** (the spawn-time refusal in "Fan ergonomics"): admit a fan only
if `remaining_children_pool ≥ n_children × min_viable_child_budget` — a *floor*,
not the full per-child cap, so worst-case reservation never strands budget, yet a
fan that can't give each child a viable minimum is refused (the model reacts by
fanning fewer/narrower).

**Attribution.** Each child records actual spend on its own `runs.cost_tokens`;
the root's reported cost is the `parent_run_id` rollup over the subtree — and the
Ops run-log tree gets per-node cost for free.

**Concurrency / impl.** In v1 (fan-in model A) children are concurrent asyncio
tasks inside the request; asyncio is cooperative (no true parallelism), so the
shared counter is a guarded integer — no DB contention. **Upgrade path:** if
children ever become cross-worker queue jobs (fan-in model B), the counter moves
to a DB atomic (`UPDATE … RETURNING`); design the in-process counter so this swap
is local. The counter + caps are pure integer arithmetic and the adapter fake
reports per-call usage, so pool depletion, per-child capping, root_reserve
survival, and the over-budget refusal are all deterministically unit-testable.

**Strawman numbers (tunable — see "concrete caps" below):** `spawn_multiplier` 3
(jerv 800k → tree ceiling ~2.4M), `root_reserve` 400k, `per_child_cap` 400k,
`min_viable_child_budget` 100k, `max_parallel` 4.

**Leaner fallback:** pure static carve (no shared counter — `n` children each
capped at `children_pool / n`, admitted if that ≥ floor) is simplest and fully
deterministic, but strands budget when children underspend and can't reallocate.
The shared-counter hybrid is one atomic int more and meaningfully better for
bursty, uneven research fans; the static carve is a clean upgradeable start if
minimal surface area is preferred for v1.

### Fan ergonomics

How the parent launches the fan and folds the results back is the load-bearing
runtime decision. Three sub-questions:

**Fan-out — how the model launches it.** `spawn_subagent` takes an **array of
`tasks`** in one atomic call, **not** N separate tool calls. One call = one budget
check, one GUI group, and — decisively — it sidesteps a real property of the live
loop: tool calls in one assistant message are dispatched **serially**
(`loop.py:531`), so N separate spawn calls would launch one-at-a-time. A single
array call launches the whole fan, and the children then run concurrently in the
background.

**Concurrency.** Children run as concurrent detached runs (each its own
`AgentLoop` with its own budget slice), bounded by the fan's `max_parallel` and
the tree agent-cap. Each streams `subagent_progress` to the GUI independently.

**Fan-in — how the parent collects and continues.** The real fork; three models,
in order of cost:

- **A. Blocking collect within the turn — recommended for v1.** The parent spawns
  the fan and the loop **awaits it**, streaming every child's progress live the
  whole time. The parent turn is already detached from the SSE socket (a dropped
  phone never kills it — `ASSISTANT.md`), so "blocking" means *the loop waits*, not
  *the user waits on a frozen socket*. When the children finish, their summaries
  fold back as **tool results inside the data boundary** and the parent runs a
  final synthesis step. One turn, one mental model, natural budget accounting, and
  it fits the existing synchronous loop with **no new re-entry machinery**. Cost:
  the turn lasts as long as the slowest child — bounded by the per-child
  wall-clock cap, and made tolerable by the live progress UI.
- **B. Fire-and-continue across turns — deferred.** The parent spawns, ends the
  turn ("dispatched 3 researchers, I'll report back"), and child completion later
  *pushes* results that trigger a synthesis turn. This needs a completion-callback
  / loop re-entry path and careful framing against #10 (it is an owner-initiated
  *continuation*, not an untrusted-origin trigger). Real machinery; not v1, but
  the data model below doesn't preclude it.
- **C. Streaming incremental join — later.** The parent is re-prompted as each
  child lands, synthesizing incrementally. Most powerful, most complex.

**Cross-cutting rules (all models):**

- **Graceful degrade.** A child that errors or times out returns a **structured
  error summary** (an observation, never a crash); the parent proceeds with the
  rest of the fan (`.filter` out the failures), mirroring the loop's
  "tool errors are observations" philosophy.
- **Cancellation cascades.** A parent `cancel` cancels its whole subtree.
- **Stable ordering.** Summaries are collected and presented in **label/index
  order, not completion order**, so the synthesis step is reproducible.
- **Budget gate.** A fan whose projected cost exceeds the remaining tree budget is
  **refused at spawn** with an actionable observation, so the model can fan fewer
  or narrower children rather than overrun the ceiling.

### Security / non-negotiables it must respect

- **#1 data/instruction boundary** — the brief and every child summary are
  **data**, never instruction; a child cannot be steered by parent prose, nor the
  parent by a child's returned text.
- **#8 least privilege, no confused deputy** — a child's tools and scope are
  always **⊆ the parent's** (settled #5): effective tools = `persona ∩ parent`,
  effective scope = `requested ∩ parent` (narrow-only); no child runs at a
  scope the parent lacked.
- **#9 controlled egress** — web research rides jerv's bounded surface only
  (SearXNG + SSRF-guarded `web_fetch`); no new raw-HTTP path. Web-sandboxed
  children hold no KB tools, so there's almost no owner data to leak into a query
  or URL.
- **#10 no untrusted-origin trigger** — a spawn happens only inside an
  owner-initiated turn, never auto-fired by note/intake/attachment content.
- **Memory** — sub-agents write **no episodic memory** and **no behavioral
  memory** (sandboxed). Durable world-knowledge a research run surfaces re-enters,
  if at all, only through the **notes door** as an owner-confirmed, agent-authored
  note (#7) — never minted as fact by a sub-agent.
- **Versioning** — personas' system prompts are versioned `.prompt` files;
  customization is the brief, not the prompt.

## GUI

Per `DESIGN.md`, both surfaces below are **mock-first**: 3–4 variants, owner
review, fixtures in `frontend/src/api/mock.ts` (default / empty / long / error /
offline), decision recorded back into `DESIGN.md` before backend wiring. A new
tool-view component is a deliberate, versioned change like adding a tool.

### In-chat: live sub-agents in the assistant bubble

- New `ChatEvent` variants — `subagent_spawned`, `subagent_progress`,
  `subagent_done` — added to the union (`frontend/src/agent/types.ts`) and folded
  in the `applyEvent` reducer (`transcript.ts`), the single extension point for
  streamed signals.
- Rendered as a **live, collapsible list** in the assistant bubble's answer block
  (`FullBrainSurface.tsx`), each sub-agent one expandable row reusing
  `LiveToolStatus` (spinner + phase + progress bar) and `TurnGlyph`
  (thinking/working animation). The eventual fan-out result lands as a registered
  **tool-view** (`agent/views/registry.tsx`) — data-only, no model-authored
  markup.

### Session manager: collapsible sub-agent tree

- `AgentSession` payload gains `parent_session_id?` / `subagent_count?`
  (`types.ts:204`).
- `SessionsPanel` groups children under their parent during bucketing and renders
  them with the **OpsCard collapsible disclosure pattern** (`DESIGN.md` §"Ops
  screen") — caret + `aria-expanded`, child `SessionRow`s slide open beneath the
  parent, each with its own live `TurnGlyph`. `ProposalTree` is the nearest
  existing precedent for a parent→children hierarchy with per-node status.
- **`activeTurn` must become a map** keyed by session id: today it's a single
  nullable value (one in-flight turn, `useFullBrain.ts:322`), but concurrent
  sub-agents mean several rows glyph at once.

## Testing

- **Loop / spawn**: the adapter fake drives scripted `tool_use` for deterministic
  multi-turn spawn → collect tests; depth/fan-out/budget caps are pure and
  unit-tested.
- **RLS**: per-table isolation test for the new lineage columns; a test proving
  child scope can only narrow and a child cannot read a parent-forbidden domain.
- **Sidecars**: `spawn_subagent` validity (valid schema, unique name/version);
  persona allowlists assert no KB tool is reachable.
- **Frontend**: reducer tests for the new `subagent_*` events; mock fixtures for
  the live panel and the collapsible tree.
- 80% backend coverage, security paths 100%, real Postgres via testcontainers,
  LLM faked (per `CLAUDE.md` #5).

## Proposed phasing (when scheduled)

1. **Spawn core** — the `spawn_subagent` tool, the three personas + `.prompt`
   files, the lineage migration + RLS tests, depth/fan-out/tree-budget
   guardrails. Detached child runs reusing the Tasks-runner path; summaries
   collected by the parent. **No new GUI yet** (children visible only in the Ops
   run-log).
2. **Live chat surface** — the `subagent_*` `ChatEvent`s, the in-chat live panel
   + tool-view summary (after the GUI mock gate).
3. **Session-tree surface** — `parent_session_id` in the payload, the collapsible
   nested rows, `activeTurn`→map for concurrent glyphs (after its own mock gate).

## Open questions for the build plan

*Settled (see above): execution model (detached + live), sub-agent access
(web-sandboxed), spawner (`jerv`), inheritance (child ⊆ parent), depth (2
sub-agent layers), fan-in model (A — blocking collect within the turn, v1), tree
budget (shared-counter ceiling + per-child reservation + root reserve, charged to
the root).*

- **Concrete caps.** Final numbers for `spawn_multiplier`, `root_reserve`,
  `per_child_cap`, `min_viable_child_budget`, `max_children_per_parent`,
  `max_total_agents_per_tree`, `max_parallel`, and per-child wall-clock — the
  strawman in "Tree budget" tuned against jerv's `budget_multiplier` and a real
  research sweep.
- **Persistence of child transcripts.** Keep full child transcripts (audit,
  re-open in the tree) or only the returned summary + run-log? Note-deletion
  cascade (#11) implications if a child ever touched note-derived content.
- **`review` web access.** Confirm `review` gets the bounded web surface for
  fact-checking (current draft says yes); `summarize` stays tool-less.
- **Fan-in B/C later.** Whether to invest in fire-and-continue (B) or streaming
  incremental join (C) after v1 ships model A.
