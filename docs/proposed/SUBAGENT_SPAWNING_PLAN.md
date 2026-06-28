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

The spawning agent (the `curator`, or a future orchestrator) reads a default
**brief template** (research / review / summarize), tweaks it for the task at
hand, and calls `spawn_subagent` to launch one or more child sessions. Each child
runs the **same `AgentLoop`** with a **web-sandboxed persona** (no knowledge-base
access, like `jerv`), a **read scope that can only narrow** relative to the
parent, its **own guardrail budget drawn from a shared tree budget**, and a
**depth counter** that refuses to spawn past 3 layers. Children run **detached
and concurrent** (the Tasks-runner pattern), stream live progress up to the
parent's chat and the session manager, and return **only a summary** as data.
The parent reads the summaries, cites them, and composes the final answer.

## Settled decisions (owner, 2026-06)

1. **Execution model: detached + live.** Children run as background sessions and
   stream live progress into the chat and the session tree; the parent collects
   summaries when they finish. (Not the simpler blocking-inline variant.)
2. **Sub-agent access: web-sandboxed (jerv-style).** Research/review/summarize
   personas read **no knowledge base** — web tools + transform only, returning
   cited summaries. This is the cheapest, safest firewall story: almost no
   personal context can ride along off-box.
3. **Nesting depth: 3 layers**, enforced structurally by the harness.

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

A single new tool, `spawn_subagent`, available only to spawn-capable personas
(initially `curator`; `jerv` itself optional). Sketch of the `.tool` params:

```
persona:   research | review | summarize     # closed set, code-defined
brief:     string                            # the task + curated context (data)
scopes:    [domain]                           # optional; MUST be ⊆ parent scope
label:     string                             # short display name for the GUI row
```

The handler:

1. **Resolves the child persona** via `agent_for(persona)` — fixed, versioned
   system prompt + tool allowlist + `reads_knowledge_base=False`.
2. **Computes the child scope** = `requested ∩ parent scope` (narrow-only; a
   request to widen is clamped, never honored — non-negotiable #8).
3. **Checks the harness caps** (depth, fan-out, tree budget — below). A refusal
   returns a structured `is_error` observation the model can react to, never an
   exception.
4. **Mints a child `agent_session`** with `parent_session_id` set and `depth =
   parent.depth + 1`, reusing `AgentSessionRepo` (the Tasks-runner path).
5. **Seeds the child conversation** with the brief as the **first user message
   inside the data/instruction boundary** (non-negotiable #1) — see "the brief".
6. **Launches the child detached** (background run, like a Task) and returns a
   **handle** inline (the child `session_id`/`run_id`), not a blocking result.
7. **Streams the child's progress** up as new `ChatEvent`s; the parent **collects
   the summary** when the child reaches `done`.

The parent can spawn several children in one turn; each is an independent
detached run. The parent turn does **not** block on them — it gets handles back,
and a later parent step (or a `collect_subagents`-style read) folds in the
summaries. (Exact fan-in ergonomics are an open question — see below.)

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

- **research**: `web_search`, `web_fetch`, `current_time` (the bounded jerv web
  surface — SearXNG + SSRF-guarded fetch). No `spawn_subagent` at depth limit.
- **review**: same web surface (optional), no mutate/KB tools.
- **summarize**: no tools (like `teacher`).

Whether research/review may themselves spawn (to nest) is gated purely by the
depth counter, not by persona — see caps.

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

Nesting × fan-out is the #1 risk (3 deep, 3-wide ≈ 39 agents). All caps are
enforced by the harness at spawn time, alongside the existing per-loop
`Guardrails` (`loop.py:106`):

- **`max_depth = 3`** — the child's `depth` rides its `ToolContext`; a spawn that
  would exceed it is refused structurally (same enforcement class as the
  dispatch-time tool allowlist).
- **`max_children_per_parent`** and **`max_total_agents_per_tree`** — fan-out and
  tree-size caps; over-cap spawns refused with an actionable observation.
- **Shared tree token budget.** Today guardrails are strictly per-loop. A tree
  needs a **root budget that children draw down from** so the whole fan-out can't
  outspend a ceiling. Each child gets its own `max_cost_tokens` slice; the root
  turn's interactive budget is never starved by descendants.
- **Wall-clock + cancellation.** A parent `cancel` cascades to its subtree (the
  detached children are cancelable like any run; `POST /chat/runs/{id}/cancel`).

### Security / non-negotiables it must respect

- **#1 data/instruction boundary** — the brief and every child summary are
  **data**, never instruction; a child cannot be steered by parent prose, nor the
  parent by a child's returned text.
- **#8 least privilege, no confused deputy** — child scope ⊆ parent scope
  (narrow-only); child tool allowlist ⊆ persona ∩ parent; no child runs at a
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

- **Fan-in ergonomics.** Does the parent poll handles, get a push when each child
  finishes (a `collect_subagents` read tool vs. auto-folded `subagent_done`
  events), or block a later step until N children complete? Affects the loop and
  the budget accounting.
- **Tree budget mechanics.** One shared counter decremented across the tree, or a
  static per-child slice carved from the root? How is a child's spend attributed
  back to the root turn's cost guardrail?
- **Should `jerv` itself spawn, or only `curator`?** And may research/review
  children spawn grandchildren, or is only the root allowed to fan out (depth
  used only as a hard ceiling)?
- **Persistence of child transcripts.** Keep full child transcripts (audit,
  re-open in the tree) or only the returned summary + run-log? Note-deletion
  cascade (#11) implications if a child ever touched note-derived content.
- **review/summarize web access.** Does `review` get the web surface for
  fact-checking, or stay tool-less like `summarize`?
