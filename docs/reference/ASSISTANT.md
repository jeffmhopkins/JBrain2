# JBrain2 — Assistant

> **Status:** Living · **Last verified:** 2026-07-07

The personal agent. This is the **binding design** for the tool-calling agent
(ROADMAP.md): a smart, tool-using assistant with durable memory — built natively
on JBrain2's existing substrate (LLM adapter, storage abstraction, RLS-scoped
Postgres, job queue, review inbox), not bolted on. **The Phase-4 core is shipped** —
the agent loop, the small tool set, Tier-A memory, Reflexion (Loop 1), the Proposal
primitive, and external connectors are all live (see `docs/archive/ASSISTANT_PLAN.md`
for the build record).

The design once staged three further self-improvement loops on top of this core —
skill learning (Loop 2), durable-knowledge + predicate-canon promotion (Loop 3),
and prompt/tool self-edit (Loop 4), plus an eval/promotion gate to admit them.
**Those loops and their promotion harness were removed**; only Loop 1 (reflexion,
shipped) remains. What stays below is the live agent and its kept primitives;
references to Loops 2–4 are retained only to record what was cut. Synthesized from
the research dossiers in `docs/archive/research/self-improving-agent/` (A landscape,
B memory, C loops, D runtime, E fit-review, F red-team).

## The paradigm in one paragraph

Steal the **lean core** of a personal assistant — file-style working memory, a
small well-shaped tool set, ephemeral self-critique (reflexion) — and **refuse
every breadth axis** (the messaging-platform / model-provider / terminal-backend /
plugin sprawl that makes such systems feel bloated). Express that core entirely on
JBrain2's existing stores: a thin in-house agent loop over the LLM adapter; and
MemGPT-style two-tier memory where the "MD files" are storage-backed rows and the
"RAG DB" is the existing pgvector hybrid search. The load-bearing rule that makes
all of it safe and keeps the wiki contract intact: **the agent's only durable
improvement is ephemeral reflexion; the notes→facts→wiki pipeline owns what is
true. The agent never gets a privileged write path into citable knowledge.**
(An earlier design layered a verified skill library and prompt/tool self-edit on
top, each gated by blast radius × reversibility; those were removed.)

## Non-negotiables for the assistant

These extend CLAUDE.md's project non-negotiables; they are binding for all agent
code. They exist because **untrusted content reaches the model** (note bodies,
intake submissions, OCR'd attachments, OwnTracks data, retrieved chunks, the
agent's own episodic memory) and **the model is the thing under attack**. Every
place a firewall decision would otherwise be made by model judgment, it is
instead enforced by RLS, by an owner confirmation, or by a fail-closed default.

1. **Data/instruction boundary (the master invariant).** All content the agent
   did not itself author is wrapped in an explicit data boundary and is **never**
   executable as instruction. The system prompt declares that no text inside the
   boundary can change the agent's policies, tools, scopes, or memory. This
   includes retrieved agent memory.
2. **Memory is read as data, never as instruction.** Retrieved episodic/semantic
   memory is presented as "here is what happened / what you know," never "here is
   what to do." Neutralizes MINJA/MemoryGraft "imitate your past success" attacks.
3. **Behavioral memory is owner-confirmed-write only.** The agent has **no
   autonomous write path** to behavioral / self-semantic memory. Such memory is
   created or changed only by an owner-issued, owner-confirmed `remember` action —
   never inferred from conversational content, never from a non-owner principal —
   and references the **owner subject only**.
4. **Episodic domain scope is fail-closed, RLS-enforced.** An episodic trace is
   scoped to the **most-restrictive domain any content in that turn touched**,
   enforced by an RLS column, not an LLM classifier. A multi-domain answer's
   episode is visible only to a session holding **all** touched scopes; it is
   never decomposed into a `general` row.
5. **No cross-domain memory composition.** Fused memory-plus-corpus retrieval is
   **owner-full-scope only** — a query runs at a single domain scope, and a turn
   that reads one domain may not stage an artifact in another.
6. **Self-improvement cannot auto-change behavior or truth.** Only ephemeral
   self-correction (Reflexion, Loop 1) is auto, and it persists nothing. Durable
   world-knowledge enters **only** as a note through normal ingestion. The agent
   has no runtime path to change its own prompts, tools, or behavior. *(The
   removed Loops 2–4 — skill promotion, durable-knowledge promotion, prompt/tool
   self-edit — and their promotion gate are gone; this rule now bounds Loop 1 and
   the note door alone.)*
7. **Agent-drafted corrections are attributed and not privileged.** Agent-authored
   notes are provenance-flagged, carry the source ID of the content that prompted
   them, get **normal (not elevated) extraction weight** when sourced from
   untrusted content, surface as a distinct review-inbox item, and are
   subject-checked and rate-limited. Elevated weight is reserved for owner-authored
   corrections.
8. **Least privilege; no confused deputy.** Every agent-internal job (reflection,
   compaction) runs at the **domain scope and principal of the
   content/session that triggered it** — never an escalation to owner scope.
   Non-owner principals (intake links, device keys) get a **default-deny,
   capture-only** tool allowlist and cannot write agent memory.
9. **Controlled egress only.** Agent **output never triggers external resource
   loads** (no markdown images/links/render-time fetches) and there is **no
   arbitrary fetch/HTTP tool**. The *only* outbound egress is the **connector
   abstraction** (below): a fixed allowlist of named, server-side, owner-configured
   upstreams called with **typed minimal inputs**, egress-minimized, cached, and
   logged. **Every off-box call is staged as an `egress` Proposal** — the owner
   approves the exact outbound payload before it leaves the box (the human is the
   final egress guard); standing per-connector approval is an optional owner
   widening, and any connector can be disabled. Location connectors are
   **local-first** so location data stays on-box (an on-box lookup egresses
   nothing and needs no Proposal).
10. **Untrusted-origin content never triggers a background job.** Any batched or
    scheduled agent processing runs only on owner-originated content, never on note
    bodies, intake submissions, or other untrusted-origin input. *(This once also
    capped a per-principal self-improvement spend budget; those pipelines were
    removed, but the no-untrusted-trigger rule stands for every remaining job.)*
11. **Purge is total.** Note deletion cascades to agent episodic memory — delete
    or redact the episode, not merely repair pointers — with a test asserting no
    agent-memory row retains content derived from a deleted note.

## What we steal, and what we refuse

**Steal (the lean core):** **two-tier memory** with self-edited working blocks
and paged archival (MemGPT/Letta); **scored retrieval** (Generative Agents); the
**curated-MD + lazy-load** discipline and **progressive disclosure** (Claude Agent
SDK); **ACI tool discipline** — few, well-shaped, feedback-rich tools (SWE-agent);
**delta-edit** memory updates that resist context collapse (ACE); and
**Reflexion/Self-Refine** as the bounded, ephemeral self-correction primitive.
*(An earlier design also stole a verified, embedding-retrieved skill library
(Voyager) as procedural memory; that was removed.)*

**Refuse (the breadth bloat that motivated this work):** no messaging-transport
sprawl (the phone PWA chat is the one interface that matters); no model-provider
or terminal-backend zoo (the LLM adapter already abstracts two backends); no
plugin/marketplace layer or migration importers; **no second datastore, broker,
or external memory/user-modeling service** (Postgres is the queue, vector store,
and workflow log); **no runtime self-code-modification** (offline, benchmarked,
owner-approved, shipped via PR); **no unbounded autonomous loop** (episodic,
human-anchored, step-capped); **no agent framework runtime** (LangChain/LangGraph/
AutoGPT — their abstractions break one-person operability); and **no code
execution in the agent**.

**Lean litmus test for any agent feature:** does it reuse the LLM adapter, the
storage abstraction, RLS-scoped Postgres, and the existing job queue / review
inbox? Does it add at most one small, well-shaped tool? Can one person still
operate and reason about it? If no — it is bloat; cut it.

## Agent runtime

A thin in-house **ReAct-style `while`-loop** over the LLM adapter (~200–400 lines
of our own code), using the providers' **native tool-calling** channel — never a
parse-your-own-action DSL, never a framework runtime. Build, not buy: the binding
constraint is operability by one person, and a dependency whose failures surface
"deep in chains" violates it. Native tool calling is also measurably more
reliable than action-parsing.

**Turn structure (one `AgentRun`).** Assemble request (system `.prompt`: persona
+ tool-use policy; memory context block; conversation; the tool schemas in scope)
→ call adapter with a declared task profile → inspect stop reason → on `tool_use`,
dispatch and append results, loop; on final text, stream the answer, run optional
verification, close the run. Independent tool calls in one message dispatch
concurrently under one RLS-scoped session; dependent calls serialize across turns.
Tool errors are returned as structured observations (`is_error: true` with an
actionable message), never unhandled exceptions — the model self-corrects on the
next turn.

**Guardrails — enforced by the harness, never trusted to the model:** `max_steps`
(~8–12), `max_cost` (the task-profile cost ceiling, summed against actual usage),
`wall_clock_timeout` (~60s interactive; slower work defers to the job queue),
`max_consecutive_tool_errors` (~3, breaks self-repair loops), and a per-session
`tool_allowlist`. Every run writes a full step log (prompt/tool versions, calls,
costs, outcome) — at Phase 4 to an agent-run table; from Phase 5 these **are**
workflow-engine `runs` rows — the audit trail for every turn.

**Long-running / expensive tools defer to the Postgres job queue** (a wiki
rebuild, a bulk re-embed): the tool enqueues a job, returns a handle inline, and
the chat turn never blocks. **Streaming to the phone:** the `/chat` endpoint emits
SSE/WS events (`text_delta`, `tool_call`, `tool_result`, `job_enqueued`, `done`)
so the PWA shows tool activity live. The turn runs **detached** from its SSE
connection: a backgrounded PWA dropping the socket never cancels it (it completes
and persists), and while it runs the PWA can **reconnect** —
`GET /chat/runs/{id}/stream?after=N` replays the in-memory frame buffer from the
last-seen offset and follows live, so thinking/render progress resumes rather than
restarting. Once the run has finished the reconnect 404s and the PWA recovers the
exchange from the persisted transcript instead. The composer's Stop is an explicit
`POST /chat/runs/{id}/cancel` (closing the stream no longer ends the turn).

**Per-conversation model pick.** The turn's model is the `agent.turn` route by
default, but the owner can steer a single conversation onto a different **on-box
model** from the omnibox (long-press a conversation tab → pick a loaded model; see
DESIGN.md). The pick rides `/chat` as a `model` field (a **local catalog id**,
validated server-side — an unknown id is ignored, never routed), and the endpoint
threads it as a per-turn `spec_override` through the router so the effort, context
window, and vision gate all reflect the chosen model. It is **turn-local**: scoped
to that conversation, never persisted on the session, and it does **not** change the
global task routing in Settings. A sub-agent the turn spawns still runs on its own
configured model — the override is the top-level loop's only.

**No standing multi-agent orchestra.** One context window holds a personal chat
task. The one exception is a narrow, **jerv-only** `spawn_subagent` escape hatch
for bounded web fan-out: `jerv` spawns a small, caps-bounded fan of
**web-sandboxed** children drawn from a closed persona set
(`research` / `review` / `summarize`) that hold **web tools only — no knowledge
base, no location, no memory** — optionally in up to two `feed`-linked waves
(depth ≤ 1). Each child returns only a summary. It is bounded web fan-out, **not**
the knowledge agent sharing its RLS-scoped tool set, and **not** a code-execution
or privilege-escalation path.

### Tools as `.tool` sidecars

Tools are co-located sidecar files mirroring the `.prompt` convention
(DEVELOPMENT.md earmarks exactly this for Phase-4 tool defs): YAML frontmatter +
a prose description body, beside the handler. Frontmatter → provider JSON schema;
body → the tool description the model reads (given the same prompt-engineering
attention as any prompt). Each carries a `version` that is **CI-guarded** (change
prose or params without a bump → build fails) and stamped on every run the tool
participates in, so a behavior change is a deliberate migration. Frontmatter also
declares `domains` (which scopes may see it), `mutating`, `side_effecting`,
`cost_class`, and `response_format` — `concise`/`detailed` text and/or a **view**
(see below). A `ToolRegistry` discovers and validates sidecars at startup (invalid
sidecar or missing handler → startup failure), and `schemas_for(scopes)` returns
only the tools in scope.

**Tool result views.** A tool may render rich UI — lab plots, tables, timelines,
appointment cards, confirm sheets — by returning a **`view`**: a schema-validated,
**data-only** payload naming a **registered first-party component** and filling its
typed slots (`{view:"lab_plot", series:[…], ref_fact_ids:[…]}`), with a
`surface` hint (`inline`/`sheet`/`dialog`). The PWA renders the named component
from a fixed registry into the chat or the shared `<Sheet>`/`<Dialog>` shell — it
is **never** model-authored HTML/markdown/URLs (that would be the exfiltration
channel #9 forbids and would let model output drive the render). Views are **data,
not instruction** (#1), render **no external resources** (#9), and carry
`fact_id`/`entity_id` refs (pointers-not-copies, citation hover-cards); their data
came from an RLS-scoped tool call so domain firewalls hold at the source.
**Interactive views never mutate directly** — a button dispatches a tool call or
stages a Proposal under the session's action policy. Adding a component is a
deliberate, versioned change (`docs/reference/DESIGN.md` "Agent tool views"), like adding a
tool.

**Each tool pays a context-and-bloat tax** (ACI discipline), but the surface has
grown well past the original core as capabilities landed — `SERVICES.md` has the
full grouped inventory. The knowledge core: hybrid `search`,
`read_note`/`read_entity`/`read_wiki`, the list tools
(`create_list`/`read_list`/`add_list_item`/`remove_list_item`/`check_list_item`),
`manage_appointment`/`read_appointment`, `propose_correction`, `propose_merge`,
plus the memory tools (`remember`/`recall`/`memory_read`/`memory_edit`).

`propose_merge` stages a duplicate-entity merge: the agent passes two entity ids
(from `find_entity`) and the leaf carries them structurally in its preview, never
in prose. On enact the merge runs through the **same** fold-and-repoint the review
inbox uses — the survivor is the more-anchored identity (the owner is never merged
away), a permanent `distinct_from` blocks it, and a re-enact is idempotent. The
agent cannot fold entities on its own (the model only ever proposed a free-text
correction before, which a same-as edge could never actually combine).

**RLS is inherited, not rebuilt.** Every handler receives the RLS-scoped session
and calls **services, never SQL** (routes→services→repositories). The domain-scope
GUC filters every query, so a `health`-scoped session physically cannot read
`finance` rows even if a tool's prose is wrong. Two layers: **visibility** (`domains`
+ session scope decide which tools are offered) is convenience; **RLS at the DB
layer** is the security boundary. Mutating tools route through the
correction-note / review-inbox path — the agent proposes, the pipeline disposes.

**Notes are the source of facts; the entity graph is the arbiter of current
truth [decided].** A note is an immutable *provenance* record — every fact traces
to one, and deleting a note purges its derived facts (docs/reference/ANALYSIS.md
"notes-as-sole-truth"). But a note's prose is the record *as captured*, frozen in
time; **what is true now** is the fact graph after the supersession and review
machinery has run — the entity layer, where a later note supersedes an old value,
a correction retracts an error, and the review inbox adjudicates conflicts. So
the retrieval tools must not let the agent read raw note prose as current truth:
`search` and `read_note` overlay a **currency check** (`SqlAnalysisRepo.
note_currency`) flagging any of a note's facts that are now superseded, retracted,
or pending review, inlining the current value and pointing at `read_entity` — the
authoritative entrance to the entity tree. The agent prefers the entity graph's
live value; the note explains *where it came from*, not *whether it still holds*.

**Testability.** The adapter fake drives the loop with scripted `tool_use` blocks
for deterministic multi-turn tests; handlers test as plain async service calls
against real Postgres via testcontainers, with the mandatory per-table RLS
isolation test; sidecar validity (compiles to a valid schema; unique name/version)
is a unit test; guardrail accounting is pure and unit-tested.

### Session capabilities — read-scope chosen up front, writes staged

Every session is a **capability, not an identity**, configured by two independent
dials that are both least-privilege by default (browsable on the **Sessions page** —
a right-swipe from the Full Brain composer, DESIGN.md):

- **Read scope, selected at session start.** The owner picks which knowledge-base
  sources — domains (`general`/`health`/`finance`/`location`/…) × subjects (me, Dad,
  …) — this session may read. That selection **sets the RLS domain-scope GUC**, so
  every query, retrieval, and tool the session runs is physically bounded to it: a
  session opened "general only" cannot read a health fact even when asked, and
  injected content in it cannot reach what the session cannot see. The default is a
  **last-used set**, seeded — for the owner, who already holds every scope — to
  **all domains** on first run; narrowing then sticks as remembered intent. Scope is
  presented as a rail you nudge, not a gate you climb (the Chats picker starts a chat
  in one tap on the last-used scope, with named presets and a Custom grid behind
  progressive disclosure — docs/mocks/session-panel-b-quick-presets.html). The
  blast-radius cost of an all-domains owner default is bounded by the parts that
  *are* the boundary: RLS scoping at the DB, writes that only ever stage Proposals,
  and the egress chokepoint that approves the exact payload — none of which the read
  dial moves. **For non-owner principals the dials stay pinned** (an intake link is
  one subject × one domain, §"Non-owner principals"); there, widening is not a
  resting state but an impossibility. The session's read scope is still the **upper
  bound** on any episode's domain, so the fail-closed episodic scoping
  (non-negotiable #4) is trivially satisfied and the write-time classifier only ever
  chooses *among the scopes already selected*.

- **Writes and sensitive actions, never standing — always staged.** Within its read
  scope a session reads freely; anything that *changes* state, or that the owner has
  flagged as approval-worthy, does not execute directly — it stages a **Proposal**
  (below). Each tool/action declares a permission class (`read` / `mutate` /
  `external` / `sensitive`); a session policy maps classes to {direct / staged /
  denied}. Default owner policy: `read` direct within scope, `mutate` and `sensitive`
  **staged**, `external` **staged as an egress Proposal** (#9). A write can target **only an in-scope
  domain** — you cannot stage a write to a domain the session cannot read.

**Chat lifecycle.** A chat the owner doesn't name is **auto-titled** from its first
exchange — a cheap one-shot summary through the LLM adapter (the `session.title`
task), best-effort and owner-only metadata, never blocking the turn that produced
it. A chat can be **archived** (a third status beside `active`/`ended`) to tidy it
out of the live Chats list without deleting it or its transcript; archiving is
reversible and orthogonal to read scope. Read scope is **adjustable after start**
(owner-only endpoint; RLS still enforces the firewall per query) — the resting
ergonomics of "a rail you nudge", not a one-shot gate. Each Chats card carries its
turn count, a resume preview (its latest turn), and how many Proposals it has
staged — derived list-view metadata, never new sources of truth.

Non-owner principals are the **same machine with the dials pinned**: an intake link
is a session whose read scope is fixed to its capability token (one subject × one
domain) and whose policy is capture-only / everything-else-denied (#8). The owner
session is just the general case where the read dial is *selectable* and writes are
*staged* rather than denied — so the whole subjects/principals/domains model
(ARCHITECTURE.md) is one mechanism, not a special case per caller.

### Agent selection — the persona chosen at session start

Full Brain mode adds a **third dial** beside read-scope and write-policy: which
**agent** the session runs as, chosen at session start (stored on the session row,
migration 0070; default `curator`, so every pre-existing session keeps today's
behaviour). An agent bundles three things the turn loop reads — the **system
prompt** that frames it, the **tool allowlist** it may call, and whether it **reads
the knowledge base** at all. The set is code-defined (`jbrain.agent.agents`). The
owner-facing conversation personas are `curator`, `teacher`, and `jerv` (below);
the box also runs **`archivist`** (a Gmail-triage persona with the `gmail_*` tools
+ an owner-only cross-session memory, no KB), the non-owner **`intake`**
interviewer (no tools, no KB), and the closed **`research`/`review`/`summarize`**
personas `jerv` spawns — the full persona table is in `SERVICES.md`.

- **`curator`** — the Full Brain personal agent (the original `agent.system`
  prompt, unchanged): every in-scope knowledge tool, narrowed to the session's
  selected domains via the RLS firewall. This is what the assistant was before
  agent selection existed.
- **`teacher`** — a Socratic homework tutor: **no tools, no retrieval.** It guides
  the learner to their own answer by questioning and never reads owner data; its
  prompt forbids handing over graded answers.
- **`jerv`** — a sandboxed general-purpose web chatbot: the internet tools
  (`web_search`, `web_fetch`), the dataless `current_time`, and the owner-approved
  `current_location`, and **no knowledge-base tools** — it runs with empty read
  scopes and writes no episodic memory, so it calls no knowledge
  tool and reads no note, entity, list, or appointment. Every turn is also given the
  **current date/time** as ambient context (non-personal). `current_location` is the
  one owner-approved exception: a `web`-gated, jerv-only on-box read of the owner's
  **coarse, coordinate-free presence** (a place name + freshness), which jerv's prompt
  forbids volunteering or sending to the web. See the sandbox bound below. The
  **`weather`** tool is a `web`-gated, jerv-only forecast lookup over a pinned
  Open-Meteo upstream (free, no key, run directly like search): it replaces the
  multi-step search-and-scrape weather flow with one call returning a summary plus a
  `weather_card` view (DESIGN.md). It sends only a public place name / city centre off-
  box — a named place geocodes by name, and "here" is resolved to a nearest-city name
  on-box first — so the owner's precise fix never leaves the box. When the
  on-box backends are configured, jerv also gets the `web`-gated, jerv-only
  **`transcribe`** (read an attached audio file via the local whisper gateway) and
  the image tools (`generate_image`/`edit_image`/`analyze_image`) — each resolves a
  chat attachment by id under the session scope, runs an on-box model, and is dropped
  from the registry when its backend is unconfigured (graceful degrade).

  **Web citations.** jerv cites a web claim with an inline `[^n]` footnote marker
  (the same convention the curator uses for notes), numbered in the order the
  sources appeared across its `web_search` / `web_fetch` calls. Each call surfaces a
  structured **`WebSource`** (the real URL it reached — a search hit's URL or the
  fetched page's final URL — never a string parsed from the model's prose), carried
  on the tool-result event beside the model text. The PWA renders each `[^n]` as a
  tappable **favicon** that opens the page. The favicon is fetched and cached
  **on-box** (`/api/agent/favicon` → `web/favicon.py`, behind the same SSRF guard as
  `web_fetch`, raster-image-only) and served from a same-origin route, so the answer
  triggers **no render-time external load** (invariant #9): the client only ever
  talks to our API, which does the controlled fetch from the source host.

Tool gating layers on top of read-scope: the registry offers a tool only if the
agent's allowlist admits it *and* the session holds its domain. The internet tools
are a new **`web` permission class** that is **opt-in** — never offered to the
default knowledge agent (so `curator` never gains arbitrary web access), only to an
agent that explicitly allowlists it (`jerv`). The allowlist is enforced **at
dispatch**, not just visibility: a tool the agent wasn't granted is refused even if
the model names it (a slip or an injection), so the boundary is structural, not a
prompt suggestion.

**The web exception to #9, and why it's bounded.** `jerv`'s `web_search`/`web_fetch`
run **directly**, not as staged egress Proposals — the deliberate, owner-approved
relaxation of "every off-box call is staged" (#9) that makes a chatbot feel like a
chatbot. The bound is the sandbox, not a promise: `jerv` holds no knowledge-base
tools, so there is almost no personal context to ride along into a query or a
fetched URL. The one deliberate exception is the **`current_location`** tool — a
`web`-gated, jerv-only on-box read returning a place name only, never a coordinate,
which jerv's prompt forbids volunteering or putting in a web query/URL; the owner
accepted this narrow location-into-jerv flow when enabling it. The ambient date/time
is non-personal. Search still goes through a **self-hosted SearXNG**
instance (pinned base URL from config, query text only) — local-first like the
on-box geocoder, so a search leaves the box only as far as SearXNG's own upstreams;
`web_fetch` is the one genuinely outbound leg, size-capped, with an **SSRF guard** —
it resolves the host and refuses any private/loopback/link-local target (and re-checks
every redirect hop), so a model-supplied URL can't read the box's own internal services
or the cloud metadata endpoint. Extraction is **on-box**: it presents as an ordinary
browser (so a bot-wall is far less likely to 403 it), runs the page through
**trafilatura** for clean main-content markdown (the dependency-free `htmltext` pass is
the fallback, and supplies the title + navigation links), and reads a linked **PDF**'s
text layer (PyMuPDF) rather than refusing it. The point is to make the on-box fetch good
enough that the model stops smuggling blocked URLs through a third-party reader like
`r.jina.ai`. For the residual case it can't handle alone — a hard bot-wall, or a
JS-rendered shell with no served content — there is a **reader fallback**
(`JBRAIN_READER_URL`): a pinned reader endpoint that web_fetch retries through. It ships
as part of the stock compose stack (an on-box, r.jina.ai-compatible renderer, like the
self-hosted SearXNG), so the default points at the on-box instance and it is on for a
stock deploy. Because Settings runs with `env_ignore_empty`, setting
`JBRAIN_READER_URL` empty does *not* disable it — an empty value is treated as absent
and falls back to the on-box `reader_url` default; to turn the fallback off, stop the
`reader` service or point `JBRAIN_READER_URL` at a non-serving endpoint. It is the sanctioned,
owner-controlled version of what the model was doing on its own — only the public target
URL travels off-box, and through an endpoint the owner pins (never one the model builds). The egress-Proposal
connectors (below) remain the rule for every *other* agent and every off-box call that
could carry owner data.

### External connectors (the egress chokepoint)

Some tasks genuinely need outside reference data — what a medication is, what a
condition means, resolving a GPS fix to an address. JBrain2 allows this through **one
egress chokepoint**, the same way every LLM call goes through the adapter and every
file through storage: a `connectors` abstraction. **No tool ever makes a raw HTTP
request** (this is a fourth chokepoint in the spirit of CLAUDE.md's three).

A **connector** is a named, owner-configured upstream with a **pinned base URL**
(config, never model-supplied), a **typed request schema**, a response parser, a
**cache policy**, a rate limit, a `domain` tag, and a consent requirement. The
connector builds the request from **typed params only** — the model fills declared
slots, never a URL, never free-form passthrough — and an **egress guard** rejects
anything beyond the declared shape, so the conversation context (and the owner data
in it) cannot be stuffed into a query string. Calls run **server-side** (api/worker),
never at render; results return as **data wrapped in the data/instruction boundary**
(#1), are **cached** in Postgres (reference data is near-static), and every call is
**logged** (connector, input hash, domain, principal). Connectors are the `external`
permission class, gated by the **Proposal primitive**: an off-box call **stages an
`egress` Proposal whose preview is the exact outbound payload** — the owner approves
*what leaves the box* before it leaves, never an intent string, so the human is the
final egress guard. The agent proposes the lookup; the call runs only on approval.
An **on-box** read that egresses nothing (the offline city geocoder,
`jbrain.citygeocode`) runs as a normal scoped tool — logged, not proposed. The owner
may grant a **standing
approval** per connector to skip per-use prompts (a deliberate widening, like the
read-scope dial) and may disable any connector outright.

The starter connectors:

| Tool | Upstream (recommended) | Typed input | Returns | Domain |
|---|---|---|---|---|
| `lookup_medication` | NLM **RxNorm/RxNav** (+ openFDA/DailyMed) — free, no-auth | `{name}` or `{rxcui}` | ingredients, dose forms, interactions, label highlights | health |
| `lookup_condition` | NLM **MedlinePlus Connect** / Clinical Tables (ICD-10/SNOMED/MeSH) | `{name}` or `{code:{system,value}}` | overview, typical management, source link | health |
| `geocode_reverse` | **on-box, offline** (bundled GeoNames cities) | `{lat,lon}` | nearest city/region/country | location |

**Geocoding is local-first by design.** A GPS fix or home address sent to an
external geocoder leaks the owner's location to a third party — exactly what the
location firewall exists to prevent. So reverse geocoding runs **entirely on-box and
offline** against the bundled GeoNames cities (`jbrain.citygeocode`) — a nearest-city
lookup with no resident service, no index, and **no egress** (it replaced the earlier
opt-in Photon container). It resolves to a **city/region/country**, not a street; an
external reverse-geocoder is only an explicit, consented fallback for a specific
street address (the staged `geocode_external` connector, or jerv's direct
owner-configured lookup). There is no on-box forward (address→coordinate) geocoder.
This is consistent with dossier G's no-external-map-**tile** rule — we still render no
basemaps; geocoding only *names* a coordinate locally, which is what lets a
`place_card` show a resolved place as text.

Medical/medicine lookups are **reference enrichment, not authority**: results are
data the agent may cite to the owner with source attribution, never minted as facts
— a looked-up interaction the owner wants to keep re-enters as an agent-authored note
through a Proposal (#7).

## Memory model

Two tiers, **distinct jobs, no overlap.** *Long-term memory* is the existing
knowledge graph (facts/entities/wiki in Postgres+pgvector) — the durable, cited,
RLS-scoped store of **what is true about the owner's life**. *Working memory* is a
small set of agent-authored Markdown blocks — the durable store of **how the agent
behaves and what it is currently doing**. The RAG DB never holds behavioral
preferences; the MD blocks never hold world-facts.

**Agent memory is metacognitive, not factual.** It holds self-knowledge
(interaction preferences, learned retrieval strategies, task state) and
**pointers** (entity/fact/note IDs) back into the cited graph — never copies of
graph content. When the agent needs a world-fact it retrieves it live and cites
it, every time.

| Memory type | Lives in | Written by | Cited? | Autonomy |
|---|---|---|---|---|
| **Working / core identity** (persona, owner preferences, behavioral rules) | `agent_memory` rows rendered as MD; small always-loaded index | Owner (policy) + owner-confirmed `remember` | No | **Owner-confirmed only** (non-neg. 3) |
| **Working / task scratchpad** (current multi-step state, plan, IDs) | `agent_memory` row, task-scoped | Agent | No | Auto; archived on task completion |
| **Semantic (self)** — behavioral learnings | `agent_memory` topic blocks, lazy-loaded | Owner-confirmed; seeded by owner corrections | No | **Owner-confirmed only** |
| **Episodic** — conversation/task traces, tool logs | `agent_episodes` rows + segregated-namespace embeddings; pointers to fact/entity IDs | Agent (auto-append) | No | Auto-write; fail-closed domain scope; nightly decay |
| **Semantic (world)** — facts about the owner's life | **NOT agent memory** — `facts`/`entities`, cited to chunks | Extraction pipeline, from **notes** | **Yes** | Pipeline + review inbox |
| **Prose knowledge** — articles | **NOT agent memory** — the wiki | Machine-only wiki builder | Yes (citation FKs) | Auto build; split/merge gated |

**MD files are rows, not paths.** There is no `MEMORY.md` on a disk the agent
opens by path (non-negotiable #2). The "files" are a presentation format over an
`agent_memory` table / storage-backed blobs (`block_kind`, `domain_id`, `body_md`,
`revision`, append-only history). The agent reads/writes them via tools on an
RLS-scoped session. The **loading discipline ports exactly** from the
curated-MD/lazy-topic pattern: a small always-loaded index, lazy topic blocks,
and **ACE-style delta edits (ADD/UPDATE/REMOVE on individual bullets), never full
rewrites** — full regeneration rots accumulated self-knowledge (brevity bias /
context collapse).

**Retrieval reuses the existing RRF hybrid search** (dense + FTS), in a
**segregated memory namespace** (a discriminator column the query filters on, and
an RLS-eligible column): agent queries can search memory + corpus together
**for the owner's full-scope session only**, but wiki builds and fact-citation
retrieval search **only** the knowledge corpus — so an episodic trace can never be
matched as a citable fact. A citation is a foreign key to a `fact`/`chunk` row;
agent-memory rows are not in those tables, so citing one is a **foreign-key
impossibility**, not a policy someone might forget. For memory namespaces, extend
RRF with **recency** (decay on `last_accessed_at`) and **importance**
(heuristic-first: owner-corrected? tool error? owner-confirmed "remember"? — an
LLM poignancy score is a deferred option). Importance from *content* is untrusted
and capped; only owner-confirmed signals raise priority.

### The bright line (the reconciliation with notes-as-sole-truth)

> **Agent memory may remember how the agent thinks and behaves. It may NOT
> remember what is true about the owner's life as an independent, citable
> assertion.** Test: *if the statement would belong in the wiki, it may not live
> in agent memory.*

- ✅ "Jeff prefers I give the raw lab number first." (behavioral)
- ❌ "Jeff's cholesterol is 210." (world-fact → a `measurement` fact from a note)
- Disambiguation memory decomposes into **(behavioral rule) + (entity-ID
  pointer)**, never entity content: store "when ambiguous, prefer Jeff's main
  office" + the entity ID, never "the office is 123 Main St."

The **one sanctioned promotion path** from agent inference to ground truth is an
**agent-authored note** through normal ingestion (provenance-flagged, normal
weight, source-attributed). `provenance` lives on the **`notes`** row, not on
`agent_memory`. JBrain2 already has the two summarizers the literature reinvents —
the fact extractor and the wiki builder; the agent does not get a third, un-cited
one for world-knowledge.

### Domain classification (an owned component)

"Inherit `domain_id` from the session scope" is **undefined for the owner**, whose
sessions carry *all* scopes — the most common case. So a small **write-time
memory-domain classifier** (owned by the memory layer) is required, and it is
**fail-closed**:

- **Episodic rows:** domain = the **most-restrictive** scope whose data the turn's
  tools actually read (RLS made the reads observable). If a turn touched `health`,
  the whole episode is `health`-scoped — never split into a `general` row by a
  classifier (non-negotiable #4).
- **Behavioral rows:** owner-confirmed only; default **into** the most-sensitive
  domain touched (ANALYSIS.md's asymmetric rule — misclassifying *into* sensitive
  is cheap; *out of* it is a leak); `general` only if provably generic; ambiguous
  consequential writes → review inbox.

Every new memory table ships the mandated RLS isolation test, including one
proving a single-scope session cannot read a multi-scope episode. **Note deletion
cascades** to episodic memory (delete/redact, not just pointer-repair), with a
test asserting no orphaned content survives.

## Reflexion (Loop 1), and the memory door

The agent's one durable-improvement primitive is **Reflexion (Loop 1)** — ephemeral
self-critique, gated by **blast radius × reversibility** at the cheapest rung: it
writes nothing, so it cannot drift. Alongside it, ordinary **memory growth** stays
inside the bright line (below): behavioral memory changes only on owner
confirmation, episodic traces auto-append within a fail-closed scope, and any
durable world-knowledge re-enters through the **notes door**, never as a privileged
agent write.

| Loop | Trigger | Autonomy boundary | Degradation guard |
|---|---|---|---|
| **1. Reflexion / self-critique** | Task profile flags a turn "critique-worthy" (citation-bearing, mutating, sensitive-domain) | **Auto.** Fully ephemeral; never persists | Mostly **deterministic** verifiers (do cited facts exist and are in-scope? do claims ground in retrieved chunks? does a mutation validate against schema?); LLM critic is a tiebreaker only (judges are noisy). Retry only if the verifier score **strictly improves**; hard cap (N=2) → runaway impossible |

**Memory / knowledge growth** is not a self-improvement loop but the ordinary
memory discipline: a chat that reveals a durable behavioral preference writes only
on **owner-confirmed `remember`** (non-neg. 3); episodic traces **auto-append**
(fail-closed scope, nightly decay); and **durable world-knowledge re-enters as a
note**, governed by the existing fact-conflict / supersession / review-inbox flow —
the agent is a **source, never an editor** of citable knowledge (agent notes get
normal weight, a distinct inbox item, a rate limit, a subject check).

*The design once added three further self-improvement loops here — **Loop 2**
(skill/playbook learning), **Loop 3** (durable-knowledge + predicate-canon
promotion), and **Loop 4** (prompt/tool self-edit) — each behind an eval/promotion
gate. **Those loops and the gate were removed.** Only Loop 1 above shipped and
remains.*

**Loop 1 in the live turn (shipped, Phase 5 Track R).** Reflexion is wired into
the only production turn — `AgentLoop.run_stream` (the `/chat` SSE path). The turn
loop tracks the answer it streamed, the sources its tools surfaced, and whether a
mutation was staged, then classifies the turn with a pure `critique_worthy(...)`
trigger (surfaced sources OR a staged/declared mutation OR a sensitive scope —
health|finance|location; greetings and general-scope chit-chat are never verified).
Two modes:

- **(b) verify-and-annotate — the default.** The answer streams normally; after
  the terminal `done`, the **pure** verifiers run over the streamed text + the
  surfaced source snippets and, if they flag something, the loop emits a tail
  `VerdictEvent` (a new `ChatEvent`; `/chat` forwards it as SSE, the PWA shows an
  "unverified claims" note). **No retry, zero extra model calls** — verify-and-
  annotate adds nothing to the per-turn cost. A non-critique turn skips it
  entirely and its stream is byte-for-byte unchanged.
- **(a) buffer-then-retry — opt-in, off by default** behind the
  `reflexion_buffer_retry` settings gate. When on and the turn is critique-worthy,
  the turn is produced non-streaming, the verifiers run, and `reflect`
  (strict-improvement adoption, hard cap N=2) may re-produce before the kept
  attempt's events stream — trading the live token stream for a spinner until
  verification clears.

Crucially, reflexion in the live turn is bound by the **ordinary per-turn cost
guardrail** (`Guardrails.max_cost_tokens`) — a live interactive turn must never be
starved by a background job. It writes **nothing durable** (Loop 1 is ephemeral):
the verdict is forwarded to the phone but never persisted to the transcript, so
there is no table, no migration, and no RLS surface.

**No runaway.** Loop 1 changes nothing durable — it can only improve a single live
answer, capped at N=2 retries, and persists no state, so there is no promotion to
compound and no drift to accumulate. Durable world-knowledge can still enter, but
only through the **notes door** under the existing fact-conflict / supersession /
review-inbox flow, and **untrusted-origin content never triggers a background
job**.

## Staging & approval (the Proposal primitive)

Several gated paths above (agent-correction, knowledge-proposal, wiki-restructure,
egress) are the same shape: **the agent wants an effect it is not privileged to
cause directly, so it stages the effect and the owner enacts it.** Promote that
shape to one first-class primitive instead of re-inventing it per feature.

**A `Proposal` is the unit of staged work, and it is a tree.** It captures: `kind`
(correction / knowledge / wiki-restructure / egress), a
**tree of staged operations** in enactable form (structured intents the relevant
machine executor will run — never prose for a human to copy), a **rendered preview
of the effect** at every node (the diff, the new revision, the article-tree change
— what the owner actually judges), full **provenance** (the conversation, notes,
attachments, or intake that prompted it, by ID), the **requesting principal and
domain scope**, and a **per-node `status`** (`staged → approved → enacted | rejected
| expired`). Every Proposal surfaces as a distinct, typed **review-inbox** item; the
inbox is the one approval surface, presented as the **Proposals page** (reached by a
left-swipe from the Full Brain composer — DESIGN.md).

**The tree is approvable in whole or in part.** Operations are organized
hierarchically — a root intent ("restructure the health wiki"), grouping nodes (one
per affected article or cluster), and atomic leaf operations — so the owner can
**approve the whole tree, a subtree, or a single leaf** in one gesture. Selection
cascades by containment (approve a node → its descendants are approved unless
individually overridden; reject a node → its subtree is rejected), and each node's
own preview and status let the owner judge effects at whatever granularity they
want. **Partial approval is dependency-safe and fail-closed:** operations declare
prerequisites (you cannot retitle an article a merge will dissolve), the executor
enacts a leaf **only when every prerequisite it depends on is also approved**, and
an approved op whose prerequisite was rejected is **held, never enacted** — so no
partial selection can leave the wiki inconsistent. Unapproved nodes simply never
run, and the privilege model is unchanged: each approved leaf is still one bounded,
owner-authorized operation.

**The privilege model, stated plainly: stage-and-approve is bounded capability
delegation, never standing privilege escalation.** The agent's own authority never
changes. Approving a Proposal authorizes **one specific staged operation, once**,
executed by the trusted machine executor under the owner's authority — it grants no
new tool, scope, or standing right, and the next equivalent action requires its own
Proposal and its own approval. The escalation is real but per-operation,
owner-initiated at the moment of approval, fully attributed, and reversible
(everything an executor produces is a versioned revision or a supersedable fact).
**No sequence of approvals can accrete into a higher resting privilege.**

**The red team's A3 is the threat this section answers**, because staging is *the*
sanctioned write lever and therefore the one worth attacking (injection-to-approval,
approval fatigue). Binding rules:

- **Untrusted-origin content can surface an analysis but can never *auto-stage* a
  Proposal** (non-negotiable #10). A restructure or correction prompted by
  note/attachment/intake content is staged only inside an owner turn, carries that
  source's attribution visibly, and gets **normal (not elevated) weight** (#7).
- **The preview is the control against fatigue:** the owner approves a *shown
  effect*, not an intent string. A Proposal without a faithful preview cannot be
  approved.
- **Proposal rationale is data, not instruction** (#1) — text the agent wrote into
  a Proposal cannot redirect the executor.
- **Proposals are rate-limited and subject-checked**; a flood is an attack signal,
  and a Proposal whose subject differs from its conversation's subject is flagged
  cross-subject (a leak signal).
- **Domain scope rides the Proposal** (#4/#8): it enacts at the scope of the content
  it touched, by the triggering principal's authority — a non-owner principal cannot
  stage wiki or behavior Proposals at all.

### Wiki analysis & restructuring

The marquee use of the Proposal primitive. On request ("clean up my health wiki")
or on a schedule, the agent **analyzes** the wiki — coverage gaps, stale or
thin-cited clusters, over-merged articles hiding two topics, under-split sprawl,
drifted titles — and **stages a restructuring plan as one Proposal tree**: grouped
by affected article or cluster, with split / merge / retitle / recluster /
rewrite-trigger **operations** as dependency-ordered leaves, each with its own
effect preview — so the owner can accept the whole cleanup, just the cardiology
subtree, or a single merge, and the builder enacts only the approved, prerequisite-
satisfied leaves.

**The agent proposes operations; the machine wiki builder enacts them as new
revisions — the agent never writes article prose.** This keeps non-negotiable #7
intact: the wiki stays machine-written, the human approves *what the machine does*,
and humans still correct *content* only via correction notes. It reuses rather than
replaces existing machinery — split/merge already gate through the review inbox and
the nightly builder already does triage rewrites; the agent's plan is the same
operations, owner-requested and batched into one Proposal instead of emerging
one-at-a-time from the nightly delta. Approval enacts the plan through the builder
under owner authority; each resulting revision is versioned and revertable.

## Mapping to existing machinery

Almost nothing new is required. The measure of fit: the assistant *composes*
JBrain2's existing parts.

| Need | Reuses | Net-new |
|---|---|---|
| Agent jobs as scheduled, audited processes | Workflow engine (`events`→`triggers`→`pipelines`→`runs`) | A few pipeline defs + nightly triggers |
| Human gating | Review inbox ("one queue for everything needing judgment") | The unified **Proposal** primitive + typed items: agent-correction, knowledge-proposal, wiki-restructure |
| Behavior versioning / rollback / audit | `.prompt`/`.tool` files + YAML `version` + CI version-bump guard + git | Reuse verbatim |
| Durable knowledge respecting the wiki contract | Notes→facts→wiki spine + correction loop + supersession + per-domain gating | `notes.provenance` flag |
| Memory retrieval | pgvector + RRF hybrid search | `agent_memory`, `agent_episodes` tables |
| Domain firewalls across all of it | RLS domain scoping + mandated isolation tests; subjects/principals/domains | The domain classifier; RLS tests per new table; the **session read-scope selector** + per-tool permission class + session action policy |
| Cheap-vs-strong routing | LLM-adapter task profiles | A couple of profiles + `.prompt` files |
| Tests | Adapter fake + testcontainers + coverage gates | Tests-with-code as usual |

**Net new:** two tables (`agent_memory`, `agent_episodes`, each `domain_id` + RLS
test), the `notes.provenance` flag, the `.tool` sidecar convention + registry, the
write-time domain classifier, and a handful of `.prompt`/pipeline defs and
review-inbox item types. **Goal: zero new runtime dependencies** — validate any new
tooling against the existing stack first, and update `scripts/dev-setup.sh` in the
same PR as any new dep, tool, or setup step (non-negotiable #8).

## Phasing

The agent **shipped in Phase 4** (✅); some kept capabilities lean on later
machinery (`runs`/scheduler = Phase 5, wiki/correction-note loop = Phase 6).
Stage accordingly — **do not describe a Phase-6 world as Phase 4**.

- **Phase 4 ✅ (shipped, on the Phase 1–3 substrate — review inbox +
  facts/supersession exist):** the thin agent loop + `.tool` registry + the small tool set + phone
  chat streaming; **Reflexion (Loop 1)** (ephemeral, needs nothing durable);
  **Tier-A memory** — `agent_memory`/`agent_episodes` with owner-confirmed
  behavioral writes, auto fail-closed episodic writes, the domain classifier, and
  the RLS tables/tests; agent-authored **notes** producing reviewable facts
  (the note→fact path is Phase 3; the wiki-citation half lands with the wiki). The
  **Proposal / stage-and-approve primitive** is introduced here as the unified shape
  for the review-inbox item types (agent-correction, knowledge-proposal).
- **Phase 5 (workflow engine):** agent runs become `runs` rows; **Reflexion
  (Loop 1)** is wired into the live `/chat` turn (Track R). *(This phase once also
  stood up an eval/promotion harness to gate the later self-improvement loops; that
  harness and those loops were removed — the note-analysis calibration evals
  (`docs/archive/CALIBRATION_LOOP.md`) remain as a CI quality guard only, never a promotion
  gate.)*
- **Phase 6 (wiki):** the **wiki analysis & restructuring** capability and its
  `wiki-restructure` Proposal flow build here on the wiki + review-inbox machinery.
- **Phase 7 (outer ring):** intake-link and device-key principals get the
  default-deny capture-only tool allowlist; confused-deputy scoping
  (non-negotiable #8) is enforced as those principals come online.

## Open questions for the implementation plan

The sequenced, codebase-grounded build-out — PRs, the new-table data model, and
resolutions to the questions below — lives in `docs/archive/ASSISTANT_PLAN.md`.

- The combined ER model (the two memory tables + namespace discriminator + episode→fact
  pointer table + `notes.provenance`) drawn explicitly with FKs.
- One mechanism or two for session compaction (mid-conversation) vs nightly
  episodic decay — both touch `agent_episodes`.
