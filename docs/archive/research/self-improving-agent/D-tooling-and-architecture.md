# Dossier D: Lean Tool-Use & Agent Runtime Architecture

**Investigation role:** Researcher D — lean agent loop and tool system for the
JBrain2 personal agent: build-vs-buy, turn structure, sidecar tool definitions,
RLS scoping, and the seams to memory and the self-improvement loops.
**Mandate constraint:** the owner wants "an easy system to integrate and develop
with" and explicitly rejects "the bloat of Hermes."
**Target stack:** FastAPI + Pydantic (async), SQLAlchemy 2, the in-house LLM
adapter (Anthropic-native + OpenAI-compatible, task profiles, fake-in-tests),
Postgres job queue, phone-first chat PWA.
**Date:** 2026-06-11
**Evidence labels:** `[web]` = sourced post-cutoff via search (URLs in §6);
`[training]` = model prior knowledge to Jan 2026.

---

## 1. Executive recommendation

**Build a thin in-house agent loop over the existing LLM adapter. Do not adopt
LangChain, LangGraph, AutoGPT, or any agent framework. Lean on the *patterns*
documented by Anthropic's Agent SDK and HuggingFace smolagents, not on their
*runtimes*.** The lean architecture:

- **Loop:** a single `while`-loop ReAct-style turn engine — call the adapter
  with the conversation + tool schemas, dispatch any tool calls, append
  results, repeat until the model stops or a guardrail trips. This is the same
  loop every modern harness reduces to once the marketing is stripped: "a
  simple while-loop that calls the model, runs tools, and repeats" `[web]`
  (Anthropic Agent SDK). Roughly 200–400 lines of our own code.

- **Native tool calling, not prompt-parsed actions.** Use the providers'
  structured `tool_use` / function-calling channel through the adapter. Native
  function calling is "more reliable than the early prompt-hacking days" `[web]`;
  parse-your-own-action schemes carry a measured failure tax (see §4). The
  adapter already abstracts Anthropic vs OpenAI-compatible — tool schemas are a
  thin addition to that same seam, so the agent never sees a provider SDK
  (non-negotiable #1).

- **Tools as `.prompt`-style sidecar files** (`*.tool` YAML frontmatter + a
  prose description body), mirroring the convention DEVELOPMENT.md already
  mandates for prompts and earmarks for Phase-4 tool defs. A registry loads them
  beside their handler module; the frontmatter compiles to the provider tool
  schema, the body becomes the description. One artifact, version-stamped,
  CI-guarded — identical to how prompts are governed today.

- **Every tool handler runs on an RLS-scoped session.** Tool dispatch is a
  thin layer over the existing routes→services→repositories stack; tools call
  *services*, never raw SQL. The session's domain-scope GUC is the same firewall
  that protects every other query (non-negotiable #3). No tool-specific security
  model is invented — domain scope is inherited, not re-implemented.

- **Single agent, no orchestrator-of-agents by default.** A single-user personal
  knowledge system fits inside one context window for essentially every real
  task. Multi-agent systems "use ~15x as many tokens as chat" `[web]` and are
  worth it only when "the task exceeds what one context window can hold" `[web]`.
  Keep one structured *sub-task* escape hatch (a `spawn_subagent` tool that runs
  the same loop with a fresh context and returns only a summary) for the rare
  fan-out — wide hybrid-search sweeps — but do not build a standing
  orchestrator/worker topology.

- **Long-running or expensive tools defer to the Postgres job queue.** The chat
  loop stays interactive; a tool that kicks off a wiki rebuild or a bulk
  re-embed enqueues a job and returns a handle, and the agent reports the handle
  to the user rather than blocking the turn.

- **Stream the loop to the phone.** Emit SSE/WS events for assistant text
  deltas, `tool_call` (name + args), `tool_result` (compact), and `done`, so the
  chat UI shows tool activity live — the phone chat is a primary interface, not
  an afterthought.

**Build-vs-buy verdict (justified in §4): BUILD.** The frameworks solved
yesterday's problem (weak models needing scaffolding). With native tool calling,
long context, and good instruction following, "framework abstractions have
become unnecessary or actively harmful to debuggability" `[web]`. For a
single-maintainer system the binding constraint is *operability by one person*,
and that constraint is violated by a dependency whose failures surface "deep in
chains" requiring you to "dig through 5+ layers of abstraction" `[web]`. A thin
loop keeps the traceback pointing at our code, keeps the LLM-adapter and
storage/RLS firewalls intact, and adds no transitive dependency surface to the
update path that `install.sh` and the supervisor must keep working.

---

## 2. Agent-loop design

### 2.1 Turn structure (one `AgentRun`)

A run is a sequence of *turns*. Each turn:

1. **Assemble request.** System prompt (a `.prompt` file: agent persona +
   tool-use policy) + memory context (provided by the memory seam, §5) +
   conversation so far + the tool schemas the registry exposes for this
   session's domain scopes.
2. **Call the adapter** with a declared task profile (model tier, max-cost,
   temperature). The agent's default profile is a strong tier; cheap sub-steps
   can declare cheaper profiles.
3. **Inspect stop reason.**
   - `end_turn` / final text → stream the answer, run optional verification
     (§2.3), close the run.
   - `tool_use` → dispatch each requested tool call (§3.2), append each
     `tool_result` block to the conversation, **loop to step 1**.
4. **Guardrail check** before re-entering the loop (§2.4). If a budget is
   exceeded, inject a terminal system message ("step/cost budget reached;
   summarize what you have") and force one final non-tool turn.

This is the gather → act → verify → repeat loop the Agent SDK documents `[web]`;
we implement it directly rather than importing it.

### 2.2 Tool dispatch within a turn

- Tool calls in a single assistant message are **independent → dispatch
  concurrently** (`asyncio.gather`) under one RLS-scoped session; dependent
  calls naturally serialize across turns because the model only sees results
  before issuing the next.
- Each dispatch is wrapped so a handler exception becomes a **structured
  `tool_result` with `is_error: true`** and an actionable message — never an
  unhandled 500 that kills the run. Anthropic's tool guidance: error messages
  should give "specific and actionable improvements," not opaque codes `[web]`,
  so the model can self-correct on the next turn (the core reliability advantage
  of ReAct's interleaved observation `[web]`).

### 2.3 Error handling & verification

- **Tool errors** are observations, not crashes. Validation failure (Pydantic
  rejects the args) → return the validation message as the tool result; the
  model retries with corrected args. Cap *consecutive* failures of the same tool
  (e.g. 3) to break repair loops — a known ReAct failure is "repetitive
  reasoning loops" on bad tool output `[web]`.
- **Verify-work step** (optional, per-tool flag): for mutating tools (create
  correction note, add list item) the loop can require a cheap confirmation read
  or surface the change to the review inbox rather than trusting blind success —
  the "verify work" leg of the SDK loop `[web]`. Mutations that change the
  knowledge graph go through the existing correction-note / review-inbox path,
  never direct edits (non-negotiable #7).

### 2.4 Guardrails (hard limits, enforced in our code, not by the model)

| Guardrail | Default | Rationale |
|---|---|---|
| `max_steps` (tool-call turns) | ~8–12 | Bounds runaway loops; phone chat tasks are shallow. |
| `max_cost` (summed adapter cost) | per task profile | Cost ceiling is already a task-profile field; the loop sums actual usage and stops. |
| `wall_clock_timeout` | ~60s interactive | Anything slower belongs on the job queue (§5). |
| `max_consecutive_tool_errors` | 3 | Breaks self-repair loops. |
| `tool_allowlist` | per session scope | RLS-scoped + capability-scoped tool set (§3.3). |

Guardrails are checked by the harness between turns; the model is *told* the
budgets in the system prompt but is never *trusted* to honor them. Every run
writes a full step log (prompt version, tool calls, costs, outcome) into the
workflow engine's `runs` table — agent runs are pipeline runs, reusing existing
execution-log plumbing and giving the self-improvement loops (§5) their training
signal for free.

---

## 3. Tool system design

### 3.1 Sidecar definition format (`.tool`, mirroring `.prompt`)

DEVELOPMENT.md: a prompt is "one artifact — prose, output JSON schema, token
budget, capability tier, and a `version` — in YAML frontmatter + a templated
body … and the same sidecar pattern is what Phase-4 tool definitions will
adopt." Proposed `*.tool` schema, co-located beside its handler
(`agent/tools/search_notes.tool` next to `search_notes.py`):

```yaml
# agent/tools/search_notes.tool
name: search_notes                # provider tool name; snake_case, namespaced
version: 1                         # bumped on any description/param change; stamped on runs
strength: low                     # adapter task tier this tool *implies* (advisory)
domains: [general, health, finance, location]   # which domain scopes may see it
mutating: false                   # true => verify-work + review-inbox path
side_effecting: false             # true => may enqueue a job rather than answer inline
cost_class: cheap                 # cheap | search | expensive(job-queue) — dispatch hint
parameters:                       # Pydantic-compatible JSON Schema -> provider tool schema
  type: object
  properties:
    query: { type: string, description: "Natural-language search query." }
    domain:
      type: string
      enum: [general, health, finance, location]
      description: "Restrict to one domain; omit to search all in-scope domains."
    limit: { type: integer, default: 8, maximum: 25 }
  required: [query]
response_format: concise          # concise|detailed enum, per Anthropic tool guidance
---
Hybrid (vector + full-text) search over the owner's notes and facts, always
domain-scoped to the current session. Returns ranked snippets with citations
back to source notes. Prefer a targeted query over a broad one; results are
truncated to the most relevant {limit} hits.
```

Conventions, grounded in Anthropic's tool-writing guidance `[web]`:

- **Frontmatter → provider schema; body → tool description.** The registry
  compiles `parameters` to the exact JSON Schema the adapter passes to each
  backend; the prose body is the description Claude/the model reads. Tool
  descriptions "should be given just as much prompt engineering attention as
  your overall prompts" `[web]`.
- **`version` is CI-guarded** exactly like prompts: change the prose or params
  without bumping `version`, CI fails. The version is stamped on every `runs`
  row the tool participates in, so a behavior change is a deliberate migration.
- **`response_format: concise|detailed`** lets the agent ask for raw IDs only
  when it needs them for a follow-up call, avoiding token waste `[web]`.
- **Few, high-impact tools, namespaced.** "Build a few thoughtful tools
  targeting specific high-impact workflows" rather than one-per-endpoint `[web]`;
  the ARCHITECTURE Agent section already names the right set: `search` (hybrid),
  `read_note`/`read_entity`/`read_fact`, `manage_list`, `manage_appointment`,
  `propose_correction`. Namespace by area (`list_add`, `list_remove`) — prefix
  scheme has "non-trivial effects on tool-use evaluations," so pick one and test
  it `[web]`.

### 3.2 Registry & dispatch

- A `ToolRegistry` discovers `*.tool` files at startup, validates frontmatter
  (Pydantic model for the sidecar itself), and binds each to its handler
  callable by `name`. Missing handler or schema-invalid sidecar → startup
  failure, not a runtime surprise.
- `registry.schemas_for(scopes)` returns the provider tool list filtered by the
  session's domain scopes and capability (§3.3) — this is what goes into the
  adapter call.
- `registry.dispatch(call, session)` validates `call.args` against the tool's
  Pydantic model, invokes the handler with the **RLS-scoped session**, and wraps
  the return into a `tool_result` block (applying `response_format` truncation,
  ≤~25k-token cap per Anthropic `[web]`).

### 3.3 RLS / domain scoping (the firewall is inherited, not rebuilt)

- **Every handler receives the RLS-scoped session** and calls *services*, not
  SQL (layering rule). The domain-scope GUC set on that session filters every
  query the tool issues — a `health`-scoped session physically cannot read
  `finance` rows, so a tool *cannot* leak across domains even if its prose is
  wrong. This is the same guarantee ARCHITECTURE makes for the whole app; the
  agent adds no new bypass.
- **Two-layer scoping.** (1) *Visibility* — `domains:` in the sidecar plus the
  session scope decide which tools are even offered to the model, so an
  out-of-scope tool is never presented. (2) *Enforcement* — RLS at the DB layer
  is the real boundary; visibility is convenience, RLS is security. A capability
  token (intake link) or device key gets a narrow tool set *and* a narrow RLS
  scope; the owner session gets all tools and all scopes.
- **Mutating tools** route through the correction-note / review-inbox machinery
  — the agent proposes, the pipeline disposes — preserving "the wiki is
  machine-written only; humans correct via correction notes" (non-negotiable #7)
  and never writing facts directly.

### 3.4 Testability with a faked LLM

- **The adapter fake drives the loop.** Per DEVELOPMENT.md "LLM calls never run
  in tests," the fake returns canned `tool_use` blocks, letting us script a
  full multi-turn run deterministically (turn 1 → call `search_notes` → turn 2 →
  final text) and assert on dispatch, args validation, and guardrail trips —
  with zero network.
- **Handlers test independently** as plain async service calls against real
  Postgres via testcontainers (the existing integration pattern) — including the
  mandatory **RLS isolation test per tool that touches a new table** proving a
  scoped session cannot reach other domains (non-negotiable #3, security paths
  at 100%).
- **Sidecar validity is a unit test:** every `*.tool` compiles to a valid
  provider schema and its `version`/`name` are unique — the analogue of the
  prompt-version CI guard.
- **Loop logic is pure-ish:** guardrail accounting (steps, cost, consecutive
  errors) is unit-tested with the fake adapter, no I/O.

---

## 4. Anti-bloat principles (what we deliberately do NOT add)

The owner's "no Hermes bloat" is an architecture requirement, not a vibe. The
evidence on what specifically goes wrong with heavy frameworks `[web]`:

1. **No LangChain / LangGraph / AutoGPT runtime.** Their abstractions were built
   to "work around model limitations"; modern "Agent SDKs work *with* model
   capabilities" `[web]`. The concrete failure modes they introduce: errors
   "thrown deep in chains" with no useful context, inability "to see exactly
   what was being sent to the model," and production debugging that becomes
   "archaeology" through "5+ layers of abstraction" `[web]`. For a one-person
   system that must stay operable, that is disqualifying. We keep tracebacks in
   our own ~300-line loop.

2. **No bespoke action-parsing DSL; use native tool calling.** We do not invent
   a "Thought:/Action:" text format to regex out of completions. Native
   structured tool use is the reliable path `[web]`; CodeAct/JSON-blob parsing
   carries a measured tax — 2.4% of traces fail to parse the very first action,
   and those traces succeed 42.3% vs 51.3% for clean ones `[web]`. We get
   CodeAct-style composition only where it's safe — the optional, sandboxed
   `spawn_subagent`/job-queue path — never as the default action channel.

3. **No standing multi-agent orchestra.** One context window holds a personal
   chat task. Multi-agent costs ~15x tokens `[web]` and helps only past the
   single-context threshold `[web]`; "today, multi-agent systems are often
   applied where a single agent would perform better" `[web]`. We keep exactly
   one narrow sub-task tool for fan-out search and nothing more.

4. **No second vector store, broker, or memory daemon for the agent.** Postgres
   already is the queue, the vector store, and the workflow log (ARCHITECTURE
   "one database, six jobs"). Agent runs are `runs` rows; long tools are queue
   jobs; memory is a sibling's Postgres-backed seam (§5). Adding infrastructure
   for the agent specifically would violate the "operable by one person"
   constraint that the whole stack is designed around.

5. **No prompt/tool strings in code.** Tools are `.tool` sidecars, versioned and
   CI-guarded, for the same reason prompts are — drift control and deliberate
   migration. A new tool is a new file, never an inline schema dict.

6. **No model-trusted guardrails.** Budgets are enforced by the harness, logged
   in `runs`, and surfaced to the user — never delegated to the model's
   self-restraint.

The throughline: **every "feature" of a heavy framework already has a
JBrain2-native home** (adapter, storage, RLS, job queue, workflow runs,
`.prompt` convention). The lean loop's job is to *compose* those, adding the
smallest possible surface.

---

## 5. Integration seams

**LLM adapter (the only model access).** The loop calls
`adapter.complete(messages, tools=registry.schemas_for(scopes), profile=...)`.
Tool schemas are passed through the adapter's existing provider abstraction
(Anthropic `tools` / OpenAI `tools`), so the agent code is provider-agnostic and
honors non-negotiable #1. The fake backend returns scripted tool calls for tests.

**FastAPI / routes→services→repositories.** Tool handlers are thin wrappers over
existing services (search, notes, lists, appointments, corrections). The agent
adds an `agent` service and a `/chat` route (streaming); it introduces no new
data path that bypasses the service/repository layering or the RLS session.

**Postgres job queue.** Tools flagged `side_effecting`/`cost_class: expensive`
enqueue a job (`SELECT … FOR UPDATE SKIP LOCKED`) and return a handle inline;
the worker runs them; the agent (or a later turn / push) reports completion. The
chat turn never blocks on a wiki rebuild or bulk re-embed. Agent runs themselves
are pipeline `runs`, so the workflow engine logs them uniformly.

**Phone chat UI.** The `/chat` endpoint streams SSE/WS events:
`text_delta`, `tool_call {name, args}`, `tool_result {summary}`, `job_enqueued
{handle}`, `done`. The PWA renders tool activity inline ("searching notes…",
"added to list"), matching the mobile-first, chat-is-primary mandate. Reconnect
resumes from the persisted run (append-oriented session storage, à la the Agent
SDK `[web]`), so a dropped phone connection doesn't lose a run.

**Memory seam (owned by a sibling — referenced only).** The loop takes a
`memory_context` block at turn assembly and may expose `remember`/`recall`
tools, but the memory *store, retrieval policy, and compaction strategy belong
to the memory researcher*. Our contract is narrow: (a) memory supplies a context
block per turn; (b) when the conversation approaches the model's context limit,
the loop calls the memory/compaction seam to summarize older turns (the Agent
SDK's automatic compaction pattern `[web]`) — preserving task objective,
mutations made, and open tool handles, dropping verbose tool transcripts. We
*invoke* compaction; the sibling *owns* what survives it.

**Self-improvement loops (sibling-owned — referenced only).** The `runs` table
(prompt versions, tool calls, costs, errors, outcomes) is the substrate the
self-improvement loop consumes to evaluate and tune `.prompt`/`.tool` artifacts.
Our contribution to that loop is purely *to emit clean, versioned, structured
run logs*; the loop's analysis, eval suite, and proposed edits are the
improvement researcher's lane. Because tool/prompt changes are version-bumped
artifacts under CI, any self-proposed change lands as a normal reviewed PR — the
self-improvement loop never hot-patches a live tool.

---

## 6. Sources

| # | Source | URL | Label | Used for |
|---|---|---|---|---|
| 1 | Anthropic — Building agents with the Claude Agent SDK | https://claude.com/blog/building-agents-with-the-claude-agent-sdk | [web] | gather→act→verify loop; SDK-provides-vs-you-build; lean while-loop; compaction; subagents |
| 2 | Anthropic — Writing effective tools for AI agents | https://www.anthropic.com/engineering/writing-tools-for-agents | [web] | few high-impact tools; namespacing; descriptions; concise/detailed; error msgs; 25k-token cap; eval |
| 3 | Anthropic — Effective harnesses for long-running agents | https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents | [web] | while-loop reduction; compaction pipeline; permission/guardrail layering |
| 4 | Anthropic — Building effective agents | https://www.anthropic.com/research/building-effective-agents | [web] | native tool use over abstractions; tool-def prompt attention |
| 5 | Anthropic — Building multi-agent research system / When to use multi-agent | https://www.anthropic.com/engineering/multi-agent-research-system | [web] | ~15x token cost; single- vs multi-agent threshold; orchestrator-worker |
| 6 | Claude — When to use multi-agent systems (and when not to) | https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them | [web] | "exceeds one context window" decision rule; single-agent default |
| 7 | HuggingFace — Introducing smolagents / CodeAgents + structure | https://huggingface.co/blog/smolagents | [web] | code-as-action 30% fewer steps; barebones lean-library philosophy |
| 8 | HuggingFace — smolagents tool-calling vs code actions (course) | https://huggingface.co/learn/agents-course/en/unit2/smolagents/tool_calling_agents | [web] | JSON tool-calling vs code-blob tradeoffs; parsing-error reliability data |
| 9 | Octomind — Why we no longer use LangChain | https://octomind.dev/blog/why-we-no-longer-use-langchain-for-building-our-ai-agents | [web] | framework debuggability failures; plain-Python case |
| 10 | MindStudio — LLM frameworks replaced by Agent SDKs | https://www.mindstudio.ai/blog/llm-frameworks-replaced-by-agent-sdks | [web] | "work around limitations" vs "work with capabilities"; abstraction-layer debugging cost |
| 11 | byaiteam — ReAct vs Plan-and-Execute for reliability | https://byaiteam.com/blog/2025/12/09/ai-agent-planning-react-vs-plan-and-execute-for-reliability/ | [web] | ReAct adaptivity, self-correction; loop failure modes |
| 12 | DEV — ReAct, Plan-and-Execute, Reflection (2026) | https://dev.to/gabrielanhaia/react-plan-and-execute-or-reflection-the-three-agent-patterns-every-engineer-needs-in-2026-355p | [web] | pattern comparison; when each fits |
| 13 | Native function calling reliability (search synthesis, 2025 analysis) | (per web search synthesis, source #11/#8 cluster) | [web] | wrong-tool/mis-formulated-call risk grows with tool count → keep tools few |
| 14 | Claude Code agent-loop docs | https://code.claude.com/docs/en/agent-sdk/agent-loop | [web] | reactive ReAct loop; harness executes, results feed next iteration |
| — | General agent/tool-calling architecture priors | (model knowledge) | [training] | ReAct/ToolCallingAgent structure, asyncio.gather dispatch, Pydantic-schema-to-tool compilation patterns |
