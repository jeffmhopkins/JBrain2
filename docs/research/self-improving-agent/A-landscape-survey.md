# Landscape Survey: Self-Improving Open-Source Assistants & Agents

**Investigation role:** Researcher A — landscape survey of self-improving
agents, distilled into paradigms as source material for a synthesized
`assistant.md`.
**Scope:** open-source / published agent designs, 2023–2026, with emphasis on
self-improvement loops, long-term + working memory, and "lean vs bloated."
**Date:** 2026-06-11
**Confidence tags:** `[web]` = found via live search this session (URL cited);
`[training]` = from training knowledge (cutoff Jan 2026), unverified this run.

---

## 1. Executive distillation — the paradigms that matter

Eight paradigms are worth carrying into JBrain2's agent design. One line each;
deep dives in §3.

1. **Skill library / procedural memory (Voyager).** The agent writes reusable
   *code skills*, verifies them, and files them in a growing, retrievable
   library. Capability compounds instead of being re-derived each session.
   *This is the spine of any real self-improvement loop.*

2. **Verbal self-reflection into episodic memory (Reflexion).** Convert
   outcome feedback into a written lesson, store it, retrieve it next time. No
   weight updates — improvement is *text the agent wrote to its future self*.

3. **Single-loop self-critique (Self-Refine).** Same model generates →
   critiques → revises within one task. Cheap, bounded, no persistence — the
   minimal unit of "self-improvement" and the safest to ship first.

4. **Scored memory retrieval + periodic reflection (Generative Agents).**
   Long-term memory as a stream; retrieval ranks by `recency × importance ×
   relevance`; a periodic job distills raw memories into higher-level insights
   written back as memory. *Maps almost 1:1 onto JBrain2's RAG + nightly wiki.*

5. **Memory tiering / LLM-as-OS (MemGPT/Letta).** Explicit core (in-context,
   self-edited) vs archival (out-of-context, paged in) memory, with tools the
   agent calls to move data between tiers. *The architecture for "MD files for
   immediate memory, RAG DB for deep memory."*

6. **Lean code-action tool use (smolagents / CodeAct).** The agent emits
   Python that calls tools, instead of rigid JSON tool-call turns. Fewer steps,
   composable, and the whole framework is ~1k LoC. *The antidote to bloat.*

7. **Agent-Computer Interface discipline (SWE-agent / OpenHands).** A small set
   of carefully-designed, feedback-rich tools beats a large pile of thin ones.
   Tool *ergonomics for the model* is a first-class design variable.

8. **Self-modifying meta-loops (Gödel Agent / ADAS / DGM / Promptbreeder /
   DSPy).** The agent rewrites its own prompts, scaffolding, or code against an
   empirical fitness signal. *Powerful and the literal meaning of
   "self-improving" — but the highest-risk, lowest-ROI tier for a single-user
   personal system. Steal the offline, gated, benchmark-driven version only.*

The through-line: **durable self-improvement = (a) a memory the agent can write
to, (b) a retrieval path that surfaces it at the right moment, and (c) a
distillation step that turns raw experience into reusable units (skills,
lessons, insights).** JBrain2 already owns (a) and (b) for *notes*; the agent
work is extending them to the agent's *own* operating memory and procedures.

---

## 2. "What is Hermes" findings

**Verdict: "Hermes" almost certainly = Hermes Agent by Nous Research**, an
open-source self-improving AI agent launched ~Feb 2026 under the tagline "the
agent that grows with you." It is a near-exact description of what the user
asked for ("very smart, tool-using, self-improving; strong self-improvement
loop; long-term memory via RAG plus MD-file local memory") — which is precisely
why the user is benchmarking against it and complaining about its *bloat*. `[web]`

### Candidates considered

| Candidate | What it actually is | Fit for "self-improving OSS assistant" |
|---|---|---|
| **Hermes Agent (Nous Research)** | OSS autonomous agent, persistent memory, autonomous skill creation, cross-session recall. Launched ~Feb 2026, grew to 140k→180k+ GitHub stars in <3 months. `[web]` | **Best fit — this is the one.** Matches every requirement the user listed. |
| **Nous Hermes / OpenHermes (model line)** | A family of fine-tuned *LLMs* (Hermes 2/3, OpenHermes on Mistral/Llama) by Nous Research. `[training]` | Poor fit — these are *models*, not self-improving *agents* with memory. Likely the namesake the *agent* borrows from. |
| **Hermes messaging / other "Hermes" repos** | Generic name reused across unrelated projects (message buses, etc.). `[training]` | No fit. Discard. |

### What Hermes Agent actually is (the thing to learn from and slim down) `[web]`

Architecture, as documented on its repo:

- **Self-improvement loop ("closed learning loop"):** autonomous *skill
  creation* after complex tasks complete; skills *self-improve during use*;
  periodic "nudges" to persist knowledge; the agent searches its own past
  conversations (FTS5 + LLM summarization) for cross-session recall.
- **Memory layers — strikingly close to the user's stated want:**
  - `SOUL.md` — persona/character definition.
  - `MEMORY.md` + `USER.md` — persistent memory & user profile (the "immediate
    local memory in MD files").
  - Markdown-file storage as the primary knowledge format.
  - FTS5 full-text search over conversation history (the cheap RAG-ish recall).
  - **Honcho** integration for "dialectic user modeling."
  - Skills in `~/.hermes/skills/`, compatible with the **agentskills.io** open
    standard.
- **The bloat (why the user balks):** a `gateway/` that bridges to **Telegram,
  Discord, Slack, WhatsApp, Signal, Email**; **40+ tools**; **6 terminal
  backends** (local, Docker, SSH, Daytona, Singularity, Modal); a `plugins/`
  layer; provider adapters for ~10+ model sources; OpenClaw migration import
  paths; MCP server plumbing. The *core* is reportedly lean (file-based, runs on
  a $5 VPS), but the surface area — integrations, backends, platforms — is
  enormous. **The bloat is breadth, not depth.**

**Takeaway for the synthesizer:** Hermes Agent is the *paradigm target* (skills
+ MD memory + self-curation + RAG recall) and simultaneously the *anti-pattern*
for scope. JBrain2 should steal Hermes' memory-and-skill core almost verbatim
in spirit, and refuse its gateway/backend/integration sprawl outright —
especially since JBrain2 already has a phone PWA, a real Postgres+pgvector RAG,
and one LLM adapter, so it needs *none* of Hermes' provider/transport breadth.

---

## 3. Per-paradigm deep dives

Format per entry: **mechanism / why it works / failure mode / steal-or-avoid
for JBrain2.**

### 3.1 Voyager — skill library (the procedural-memory spine) `[web]`

- **Mechanism.** Three parts: (1) an automatic curriculum proposing next tasks,
  (2) an *ever-growing skill library* of executable code, (3) an iterative
  prompt loop that writes code → runs it → feeds errors/environment feedback
  back → **self-verifies completion before filing the skill.** Skills are
  indexed by an embedding of their description and retrieved for related tasks.
  (arXiv 2305.16291)
- **Why it works.** Skills are *temporally extended, interpretable, and
  compositional* — new skills call old ones, so capability compounds and
  catastrophic forgetting is avoided. Verification-before-storage keeps the
  library trustworthy.
- **Failure mode.** Garbage-in: an unverified or wrongly-verified skill
  poisons the library and propagates into everything that composes it. Skill
  retrieval can mis-fire (wrong skill for a superficially-similar task). The
  auto-curriculum can wander off-mission.
- **Steal / avoid.** **STEAL the skill library as the agent's procedural memory
  — verified, embedded, retrievable, composable.** This is the single most
  important pattern. **AVOID** the open-ended auto-curriculum (JBrain2's tasks
  come from the owner, not self-generated exploration). **Hard-gate skill
  acceptance** behind verification + (given the non-negotiables) the review
  inbox before a skill is trusted.

### 3.2 Reflexion — verbal self-reflection into episodic memory `[web]`

- **Mechanism.** After a failed/low-reward trial, a self-reflection LLM turns
  the outcome into a concise *verbal lesson* ("I assumed X; next time check Y
  first") stored in an episodic memory buffer; that text is prepended on the
  next attempt. Short-term memory = current trajectory; long-term = accumulated
  reflections. No weight updates. (arXiv 2303.11366)
- **Why it works.** Converts sparse scalar feedback into a dense *semantic
  gradient* the model can actually act on, and persists it. SOTA on code-gen
  benchmarks at the time.
- **Failure mode.** Reflections can be wrong, vague, or over-general
  ("be more careful"), polluting future context. Unbounded buffers blow the
  context window. No mechanism to *forget* a stale lesson.
- **Steal / avoid.** **STEAL: a "lessons" memory** — when an agent action fails
  or the owner corrects it, write a one-line lesson, embed it, retrieve it for
  similar future tasks. **AVOID** unbounded accumulation: lessons need
  recency/usefulness decay and supersession, mirroring JBrain2's existing fact
  `superseded_by` chains. Lessons should be *citeable and reviewable*, not silent
  context injections.

### 3.3 Self-Refine — single-loop self-critique `[web]`

- **Mechanism.** One LLM: generate → give itself feedback → refine → repeat a
  few rounds. No training, no external data, no persistence. ~20% absolute
  average task-quality lift in the paper. (arXiv 2303.17651)
- **Why it works.** Critique is easier than generation; a second pass catches
  obvious errors cheaply within a single task.
- **Failure mode.** Diminishing/negative returns past ~1–2 rounds; the model
  can "refine" a correct answer into a wrong one (over-editing); cost multiplies
  per round; with no ground truth it can converge on confident nonsense.
- **Steal / avoid.** **STEAL as the default, lowest-risk self-improvement
  primitive** for agent outputs (e.g. a drafted correction note or a list edit
  gets one critique pass). **AVOID** treating it as "self-improvement" in the
  durable sense — it improves *one output*, learns *nothing* across sessions.
  Cap at 1–2 rounds; require a stop condition.

### 3.4 Generative Agents — scored retrieval + periodic reflection `[web]`

- **Mechanism.** A **memory stream** of natural-language observations.
  Retrieval scores every memory as
  `α·recency + α·importance + α·relevance` and surfaces the top-k.
  Periodically a **reflection** step asks the LLM to synthesize recent memories
  into higher-level insights, which are written *back* into the stream as new,
  retrievable memories. (arXiv 2304.03442)
- **Why it works.** Importance weighting stops trivia from crowding out salient
  events; recency keeps context current; reflection turns a flat log into a
  layered understanding that compounds.
- **Failure mode.** LLM-assigned importance scores are noisy; reflection can
  hallucinate spurious patterns; tuning the three α weights is fiddly; cost of
  periodic reflection scales with memory volume if not bounded.
- **Steal / avoid.** **STEAL the retrieval-scoring triad and the periodic
  reflection job — JBrain2 already has the substrate.** Hybrid search (dense +
  FTS, RRF) is the relevance term; add recency and an importance signal. The
  **nightly incremental wiki build *is already a reflection step*** over notes;
  the agent should get its own analogous nightly "distill my recent
  interactions into durable user-model/lessons" pass. **AVOID** standing up a
  separate memory store — reuse Postgres/pgvector and the existing job queue.

### 3.5 MemGPT / Letta — memory tiering, LLM-as-OS `[web]`

- **Mechanism.** Two tiers: **core memory** (small, lives in the context
  window, RAM-like, the agent self-edits via `core_memory_append` /
  `core_memory_replace`) and **external/archival memory** (out-of-context, disk-
  like, paged in via search tools). The agent *decides* what to promote/evict,
  managing its own context like an OS manages RAM↔disk.
- **Why it works.** Gives an effectively unbounded memory under a fixed context
  window; the agent curates what stays "hot." Self-editing means it learns the
  user over time without retraining.
- **Failure mode.** **Memory quality = model judgment.** If the model fails to
  save something, it's gone; it can also overwrite good core memory with noise.
  Self-editing loops can thrash. No external guarantee of correctness.
- **Steal / avoid.** **STEAL the tiering as the literal blueprint for the user's
  "MD files (immediate) + RAG DB (deep)" want:** treat `MEMORY.md`/`USER.md`-
  style files as **core memory** (always in context, agent-editable via tools),
  and the pgvector store as **archival memory** (paged in via hybrid search).
  **AVOID** fully trusting model self-edits given JBrain2's "notes are the sole
  source of truth" rule — core-memory edits about *facts* must round-trip
  through notes/correction-notes, not be silently mutated. Keep agent
  operational memory (preferences, lessons, skills) separate from the
  note-derived knowledge graph so self-edits never corrupt sourced truth.

### 3.6 smolagents / CodeAct — lean code-action tool use `[web]`

- **Mechanism.** Instead of one-tool-per-turn JSON, the agent writes a snippet
  of **Python that calls tools as functions**, executed in a sandbox; results
  bind to variables and flow into the next step. CodeAct ("Executable Code
  Actions Elicit Better LLM Agents") is the underlying result; smolagents is the
  ~1k-LoC reference implementation. `[web]`
- **Why it works.** One code block can express loops, conditionals, and
  multi-tool composition that would take many JSON turns — *fewer steps, fewer
  round-trips, more reliable state*. The framework stays tiny.
- **Failure mode.** Arbitrary code execution = a security surface; needs real
  sandboxing. Weaker models write buggy code; harder to constrain than a fixed
  JSON schema. Can over-reach (one block doing too much, hard to audit).
- **Steal / avoid.** **STEAL the lean philosophy and, selectively, code-actions
  for multi-step internal composition.** But **AVOID** unsandboxed code
  execution in JBrain2's internet-facing `api` — the architecture is explicit
  that the api never even holds the Docker socket. If code-actions are adopted,
  they belong in a constrained worker sandbox with tools that respect RLS, not a
  free Python interpreter. For most agent actions, JBrain2's curated tool set
  (search, read note/fact/entity, manage lists/appointments, propose correction
  notes) is the right, auditable surface — keep it small (§3.7).

### 3.7 SWE-agent / OpenHands — Agent-Computer Interface discipline `[web]`

- **Mechanism.** SWE-agent's core finding: a carefully designed **Agent-Computer
  Interface (ACI)** — a *small* set of tools (editor, shell, test-runner) shaped
  for how an LLM reads and acts, with rich, structured feedback — drives most of
  the performance. OpenHands generalizes this into an event-driven platform of
  sandboxed tools (edit/run/browse). `[web]`
- **Why it works.** The model's bottleneck is often the *interface*, not
  intelligence: terse, predictable tool outputs and good error messages let it
  recover; sprawling or noisy tools waste context and induce errors.
- **Failure mode.** Over-broad tool sets dilute the model's choices and bloat
  context; poorly-designed feedback (giant unstructured dumps) drowns the agent.
- **Steal / avoid.** **STEAL the ACI mindset: treat every agent tool's
  signature, output shape, and error text as a deliberate design artifact;
  prefer few well-shaped tools over many thin ones.** This is JBrain2's concrete
  defense against Hermes' 40-tool sprawl. **AVOID** OpenHands' generalist
  edit/shell/browse breadth — JBrain2's agent acts on a *bounded* domain
  (knowledge + lists + appointments), and broad system tools would violate both
  the security model and the lean goal.

### 3.8 Self-modifying meta-loops — Gödel Agent / ADAS / DGM / Promptbreeder / DSPy `[web]`

- **Mechanism.** The agent improves *itself*, not just its outputs:
  - **Promptbreeder:** evolutionary search over task-prompts *and* the
    mutation-prompts that mutate them, scored on a training set. (arXiv 2309.16797)
  - **DSPy:** declarative pipelines + an optimizer that compiles/tunes prompts
    against a metric — "prompt engineering as a learnable program." `[web]`
  - **ADAS (Meta Agent Search):** a meta-agent programs *new agents* in code,
    archiving discoveries; the meta-agent improves the *target* agent (so it is
    not strictly self-improving). (arXiv 2408.08435)
  - **Gödel Agent:** a self-referential agent that rewrites its *own* logic/
    modules at runtime, guided only by high-level objectives. (arXiv 2410.04444)
  - **Darwin Gödel Machine (Sakana et al., 2025):** population/archive-based
    evolution where agents rewrite their own code and are kept by *empirical
    benchmark fitness* (SWE-bench 20%→50%). The pragmatic, proof-free heir to
    the Gödel Machine. (arXiv 2505.22954)
- **Why it works.** Optimizes against a real metric rather than human intuition;
  can discover prompt/scaffold improvements humans wouldn't. DGM/ADAS show
  measurable, compounding gains when there's a clean benchmark.
- **Failure mode.** **Highest risk tier.** Needs a trustworthy fitness signal —
  without one, it optimizes for the metric's blind spots (reward hacking) or
  drifts. Self-code-rewrite is a safety and reproducibility nightmare
  (unbounded behavior change, hard to roll back). Expensive (populations,
  benchmark runs). The classic AutoGPT/BabyAGI failure family lives here at the
  uncontrolled end: goal drift, infinite loops, plan re-invention (§4).
- **Steal / avoid.** **STEAL only the *offline, gated, benchmark-driven*
  version:** treat agent prompts and skills as artifacts that can be A/B-tested
  against a held-out eval set, with improvements proposed → measured → applied
  *with owner approval*, never live self-rewrite. DSPy-style *prompt
  optimization against a metric* is the safe, high-value slice. **AVOID
  runtime self-modification of the agent's own code/logic** entirely for a
  single-user personal system: the ROI is low, the blast radius is the user's
  whole knowledge system, and it violates the "CI green, branch+PR, reviewable"
  non-negotiables. Recursive self-improvement is a research toy here, not a
  feature.

### 3.9 Anti-pattern reference — AutoGPT / BabyAGI lineage `[web]`

- **Mechanism.** Early (2023) fully-autonomous loops: decompose a goal into
  tasks, execute via tools, append results, repeat — minimal memory, minimal
  verification.
- **Why it (sort of) worked.** Demonstrated the autonomous tool-loop concept and
  seeded the whole field.
- **Failure mode (the lesson).** *Documented* failures: **goal drift** (wandering
  off the objective, burning tokens), **infinite loops / loop stalls**
  (perfection-seeking re-refinement), **plan re-invention** (no durable task
  memory → re-deriving the plan in circles), hallucinated plans, weak retries.
  "API orchestration on top of GPT," not cognition.
- **Steal / avoid.** **AVOID the unbounded autonomous loop.** JBrain2's agent
  should be *episodic and human-anchored*: the owner asks, the agent acts within
  a bounded tool set, durable changes route through the review inbox. **STEAL
  the negative lessons as guardrails:** hard step/iteration caps, loop
  detection, persistent task state (so it never re-invents a plan), and a clear
  termination/escalation condition.

### 3.10 Claude Agent SDK patterns (the host environment) `[web]`

- **Mechanism.** Patterns JBrain2 runs *inside* and can mirror: **subagents**
  (separate context windows for parallel/again-isolated work, preventing context
  pollution); **Skills** with **progressive disclosure** (only a skill's
  name+description is preloaded; the body loads on demand — unbounded skill
  content at near-zero idle context cost); **CLAUDE.md** as persistent project
  memory; a **memory tool** + `MEMORY.md` (first ~200 lines/25KB preloaded, agent
  curates when it overflows) surviving across sessions and compaction. `[web]`
- **Why it works.** Progressive disclosure solves the core tension between "lots
  of capabilities" and "small context." Subagents keep the main loop clean.
  Curated `MEMORY.md` is exactly the "immediate local memory in MD files" want,
  with a built-in size discipline.
- **Failure mode.** Skill/subagent metadata still consumes some always-on
  context; too many skills/subagents re-creates bloat at the metadata layer.
  Memory curation quality depends on the model.
- **Steal / avoid.** **STEAL progressive disclosure for skills (load on demand,
  keep idle context tiny) and the curated-`MEMORY.md`-with-a-size-cap pattern
  for core memory.** **STEAL subagent isolation** for expensive sub-tasks (e.g.
  a focused retrieval pass) so they don't pollute the chat context. This is the
  cleanest lean blueprint available, and it's the very environment JBrain2's
  tooling already lives in.

---

## 4. The "lean vs bloated" axis — concrete recommendations

The user's complaint about Hermes is **breadth bloat**, not core complexity.
Hermes' *core* (file memory + skills + FTS recall) is lean; its *surface*
(6 messaging platforms, 6 terminal backends, 40+ tools, plugins, ~10 providers,
migration importers, Honcho) is enormous. JBrain2 must keep the core and refuse
the surface. Concretely:

**Keep (the lean core that delivers the user's wants):**

1. **One memory model with two tiers**, reusing existing infra:
   - *Core / immediate memory* = a small curated set of MD-style blocks
     (`USER.md`/preferences, `LESSONS.md`, an agent persona) — agent-editable
     via tools, size-capped (Claude-SDK style ~25KB preload), always in context.
   - *Deep / long-term memory* = the **existing Postgres+pgvector hybrid search**.
     No new vector DB, no new store. (Architecture's "one database, six jobs"
     principle already forbids a second store.)
2. **A verified skill library** as procedural memory (Voyager spine), but
   acceptance-gated through the **review inbox** rather than auto-trusted.
3. **A periodic reflection job** on the **existing job queue** (the nightly
   pattern the wiki already uses) to distill recent interactions into lessons /
   an updated user model.
4. **A small, well-shaped tool set** (SWE-agent ACI discipline): the
   Phase-4 tools (hybrid search, read note/entity/fact, manage lists/
   appointments, propose correction notes) — and resist growth. Every new tool
   pays a context-and-bloat tax.
5. **Self-improvement as the cheap, bounded primitives first:** Self-Refine
   (1–2 rounds) on outputs; Reflexion-style lessons on failures/corrections.
   Defer everything recursive.

**Refuse (the bloat axes):**

- **No transport sprawl.** JBrain2 has *one* interface that matters: the phone
  PWA chat. No Telegram/Slack/Discord/WhatsApp/Signal/Email gateway. (If ever
  wanted, it's one optional adapter, not six.)
- **No provider/backend sprawl.** The LLM adapter already abstracts two backends
  per the non-negotiables; do not add a provider zoo or 6 terminal backends.
- **No plugin/marketplace layer, no migration importers** (OpenClaw etc.) — pure
  scope that a single-user system never needs.
- **No runtime self-code-modification** (Gödel/DGM live-rewrite). Self-tuning is
  offline, benchmarked, owner-approved, shipped via PR — per the non-negotiables.
- **No second datastore, no broker, no separate memory service** (no Redis, no
  standalone vector DB, no Honcho-style external user-modeling service) — reuse
  Postgres.
- **No unbounded autonomous loop** (AutoGPT lineage). Episodic, human-anchored,
  step-capped.

**Lean litmus test for any agent feature:** *Does it reuse the LLM adapter, the
storage abstraction, RLS-scoped Postgres, and the existing job queue/review
inbox? Does it add at most one small, well-shaped tool? Can a single person still
operate and reason about it?* If no — it's Hermes-style bloat; cut it.

**The synthesis to hand off:** JBrain2's winning design is *Hermes' memory-and-
skill core, expressed natively on JBrain2's existing Postgres-RAG + job-queue +
review-inbox substrate, with MemGPT-style tiering for the "MD-files-plus-RAG"
want, Generative-Agents-style nightly reflection, a Voyager skill library gated
by review, Reflexion/Self-Refine as the bounded improvement primitives, and an
SWE-agent-disciplined tiny tool set — and an explicit, standing refusal of every
breadth axis (transports, providers, backends, plugins, second stores, live
self-rewrite) that makes Hermes feel bloated.*

---

## 5. Sources

| # | Source | URL | Tag |
|---|---|---|---|
| 1 | Hermes Agent — Nous Research repo (architecture, memory, skills, bloat surface) | https://github.com/nousresearch/hermes-agent | [web] |
| 2 | Hermes Agent — NVIDIA blog ("unlocks self-improving AI agents") | https://blogs.nvidia.com/blog/rtx-ai-garage-hermes-agent-dgx-spark/ | [web] |
| 3 | Hermes Agent — guide (self-improving assistant overview) | https://tosea.ai/blog/hermes-agent-self-improving-ai-guide | [web] |
| 4 | Nous Hermes / OpenHermes are *models*, not the agent | (general knowledge of Nous Research model line) | [training] |
| 5 | Voyager: Open-Ended Embodied Agent with LLMs | https://arxiv.org/abs/2305.16291 | [web] |
| 6 | Voyager code | https://github.com/minedojo/voyager | [web] |
| 7 | Reflexion: Language Agents with Verbal Reinforcement Learning | https://arxiv.org/abs/2303.11366 | [web] |
| 8 | Self-Refine: Iterative Refinement with Self-Feedback | https://arxiv.org/abs/2303.17651 | [web] |
| 9 | Generative Agents: Interactive Simulacra of Human Behavior | https://arxiv.org/abs/2304.03442 | [web] |
| 10 | MemGPT / Letta — memory tiering, self-editing memory, LLM-as-OS | https://www.letta.com/blog/memgpt-and-letta | [web] |
| 11 | Letta docs — `core_memory_append` / `core_memory_replace` tools | https://docs.letta.com/guides/legacy/memgpt-agents-legacy/ | [web] |
| 12 | smolagents — barebones (~1k LoC) code agents | https://github.com/huggingface/smolagents | [web] |
| 13 | CodeAct — "Executable Code Actions Elicit Better LLM Agents" (via smolagents blog) | https://huggingface.co/blog/smolagents | [web] |
| 14 | SWE-agent / Agent-Computer Interface + OpenHands platform | https://arxiv.org/html/2407.16741v3 | [web] |
| 15 | Promptbreeder: Self-Referential Self-Improvement via Prompt Evolution | https://arxiv.org/abs/2309.16797 | [web] |
| 16 | ADAS: Automated Design of Agentic Systems (Meta Agent Search) | https://arxiv.org/abs/2408.08435 | [web] |
| 17 | Gödel Agent: Self-Referential Recursive Self-Improvement | https://arxiv.org/abs/2410.04444 | [web] |
| 18 | Darwin Gödel Machine (Sakana et al., SWE-bench 20%→50%) | https://arxiv.org/abs/2505.22954 | [web] |
| 19 | Darwin Gödel Machine — Sakana writeup | https://sakana.ai/dgm/ | [web] |
| 20 | AutoGPT/BabyAGI documented failure modes (goal drift, loops, plan re-invention) | https://github.com/vectara/awesome-agent-failures/blob/main/docs/case-studies/autogpt-planning-failures.md | [web] |
| 21 | Claude Agent SDK — subagents, Skills + progressive disclosure | https://docs.claude.com/en/docs/agent-sdk/subagents | [web] |
| 22 | Claude — Agent Skills (progressive disclosure) | https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills | [web] |
| 23 | Claude — Memory tool + MEMORY.md curation/compaction | https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool | [web] |
| 24 | DSPy — declarative pipelines / prompt optimization against a metric | (framework knowledge; corroborated in search §1) | [training] |

**Confidence note for the synthesizer:** Hermes Agent specifics (§2) are `[web]`
from its repo and corroborating coverage; the *distinction* between Hermes Agent
(the agent) and Nous/OpenHermes (the model line) rests partly on `[training]`
knowledge of Nous Research's product history — treat the "best fit = Hermes
Agent" conclusion as high-confidence and the model-line aside as
context-only. All paradigm papers (§3) are primary-source arXiv `[web]`.
