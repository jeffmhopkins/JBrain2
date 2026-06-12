# Memory Architecture for a Self-Improving JBrain2 Agent

**Investigation role:** Researcher B — memory architecture (the two-tier design:
RAG-DB long-term memory + local MD working memory).
**Swarm:** parallel design dossier; source material for a synthesized
`assistant.md` paradigm doc.
**Scope seam:** procedural memory / skills are a *sibling researcher's* lane —
this dossier defines the seam and stops at it.
**Date:** 2026-06-11
**Claims labeled** `[web]` (post-cutoff, cited) **vs** `[training]` (my Jan 2026
prior). URLs in §6.

---

## 1. Executive recommendation — the memory model in five bullets

1. **Two tiers, distinct jobs, no overlap.** *Long-term memory* is the existing
   knowledge graph (facts/entities/wiki) in Postgres+pgvector — the durable,
   cited, RLS-scoped store of *what is true about the owner's life*. *Working
   memory* is a small set of agent-authored Markdown blocks — the durable store
   of *how the agent should behave and what it is currently doing*. The RAG DB
   never holds behavioral preferences; the MD never holds world-facts. This split
   is the whole design.

2. **Agent memory is metacognitive, not factual.** The agent must NOT distill
   note-content into a private store — that would create a shadow source of truth
   that bypasses citations (the cardinal sin, see §4). Agent memory holds
   *self-knowledge*: interaction preferences, learned retrieval strategies,
   task/playbook state, and pointers (entity/fact/note IDs) *back into* the cited
   graph. When the agent needs a world-fact, it retrieves it live and cites it —
   every time.

3. **MD blocks live behind the storage abstraction, addressed not pathed.** The
   "local MD files" are content-addressed blobs / a dedicated `agent_memory`
   table rendered as Markdown, scoped per (owner, domain), self-edited via
   delta operations (ADD/UPDATE/REMOVE on individual bullets), never full
   rewrites — borrowing MemGPT self-editing `[web]` and ACE's anti-collapse delta
   curation `[web]`. No raw filesystem paths anywhere (CLAUDE.md non-negotiable #2).

4. **Retrieval is the existing RRF hybrid search, reused.** No second retrieval
   stack. Episodic conversation traces and distilled semantic memories are
   chunked, embedded, FTS-indexed, and domain-scoped exactly like notes — fused
   by Reciprocal Rank Fusion — but in *segregated memory namespaces* so agent
   memory can never be cited as a primary source in a wiki article.

5. **Auto-write, human-gate the consequential.** The agent may auto-append
   episodic traces and auto-update its own working-memory blocks (cheap,
   reversible, owner-visible). But any agent "learning" that wants to become a
   *world-fact* must exit the memory system entirely and re-enter as a
   **correction note** through normal ingestion — the only sanctioned path from
   agent cognition to ground truth.

---

## 2. Survey of memory paradigms (mechanism / fit for JBrain2)

### 2.1 MemGPT / Letta — tiered memory as virtual OS

**Mechanism** `[web]`: OS-inspired hierarchy. *Main context* (the fixed window:
read-only system instructions, a FIFO of recent turns, a writable scratchpad) is
RAM; *external context* (recall storage = full conversation history, archival
storage = vector DB) is disk. The agent **self-edits** labeled memory blocks via
tool calls in its normal loop and **pages** data in/out of the window when it
overflows — `memory_replace`, `archival_insert`, `conversation_search`.

**Fit:** *High, and directly shapes our two-tier split.* JBrain2's "local MD
working memory" maps onto MemGPT's editable core blocks; the "RAG DB long-term
memory" maps onto archival storage — except JBrain2 *already has* a far richer
archival tier (the cited fact graph) than MemGPT's flat vector store. We take the
self-editing-block and paging *mechanisms* but keep our archival tier as the
existing knowledge pipeline, not a parallel one. **Caveat** `[training]`:
MemGPT's archival memory is an *un-cited* free-text dump — adopting it wholesale
would violate notes-as-sole-truth. We adopt the control plane, not its data
plane.

### 2.2 Generative Agents (Park et al.) — memory stream + reflection

**Mechanism** `[web]`: an append-only *memory stream* of natural-language
observations. Retrieval scores each memory by a weighted sum of **recency**
(exponential decay since last access), **importance** (an LLM-assigned 1–10
"poignancy" at write time), and **relevance** (embedding similarity to the
current query). **Reflection**: periodically the agent synthesizes high-importance
recent memories into higher-level inferences, stored back in the stream and
themselves retrievable — a recursive abstraction tree.

**Fit:** *Medium-high, selectively.* The recency/importance/relevance triad is
the right *ranking signal* for episodic agent memory and complements our RRF
(which is relevance-only) — add recency-decay and an importance score as RRF
inputs for the memory namespaces. **But JBrain2 already has a superior, audited
"reflection" engine: the wiki/fact pipeline.** Generative-Agents reflection
produces *un-sourced* generalizations — exactly the shadow-truth we forbid. So:
reuse the *retrieval scoring*, route any reflection-shaped insight about the
*world* into the note→fact→wiki path, and restrict in-memory reflection to
*self-knowledge* synthesis ("the owner dislikes long answers") which is legitimately
agent-internal.

### 2.3 A-MEM — agentic, Zettelkasten-style self-organizing memory

**Mechanism** `[web]`: each interaction becomes a structured *note* with
LLM-generated keywords, tags, and a contextual description; notes are **linked**
to semantically related prior notes (Zettelkasten), and existing notes' contexts
are **evolved/updated** when a new note connects to them. No rigid predetermined
schema — structure emerges from links.

**Fit:** *Conceptually aligned but redundant with our graph.* JBrain2 already has
a typed, link-rich, self-evolving knowledge structure — the entity/fact property
graph with `superseded_by` chains and `distinct_from` edges (ANALYSIS.md). A-MEM
is essentially "a worse version of what the fact pipeline already does," minus the
citations and RLS. **Takeaway:** don't build A-MEM for world-knowledge; the graph
*is* our A-MEM. Borrow only the *link-on-write* idea for the *episodic* layer:
when an agent task references entity/fact IDs, store those as explicit links from
the episode to the graph — making episodes navigable without copying graph content.

### 2.4 Episodic vs semantic vs procedural; surveys (MIRIX, "Memory in the Age
of AI Agents")

**Mechanism** `[web]`: modern surveys converge on a typology — **Working** (active
context), **Episodic** (time-stamped event/interaction traces), **Semantic**
(distilled, timeless facts/knowledge), **Procedural** (encoded skills/playbooks),
plus Resource/Knowledge-Vault variants (MIRIX). A recurring warning: *systems that
summarize at write time collapse distinct episodes into semantic generalizations,
destroying the episodic signal before it can be used.* — keep episodic raw,
distill lazily.

**Fit:** *This typology is our organizing skeleton.* Mapping (detail in §3):
- **Working** → local MD blocks (agent identity, prefs, current-task scratchpad).
- **Episodic** → conversation/task traces, in a dedicated Postgres table +
  memory-namespace embeddings.
- **Semantic (world)** → **does not live in agent memory at all** — it lives in
  facts/wiki. This is the load-bearing decision.
- **Semantic (self)** → distilled behavioral knowledge, in MD blocks.
- **Procedural** → **sibling researcher's lane** (skills as files). Seam in §3.4.

### 2.5 Claude Code CLAUDE.md / auto-memory — local MD working memory

**Mechanism** `[web]`: two complementary systems. *CLAUDE.md* = human-written,
loaded in full every session, behavioral instructions. *Auto memory* = agent
writes notes to itself (`MEMORY.md` index + on-demand topic files); only the first
~200 lines / 25KB of the index load at session start, topic files load lazily via
file tools. Both are "context, not enforced configuration." Imports via `@path`;
`/compact` re-reads project CLAUDE.md from disk so instructions survive
compaction.

**Fit:** *This is the literal blueprint for JBrain2's "local MD working memory"
tier.* The CLAUDE.md/auto-memory split = our human-authored-policy /
agent-authored-learnings split. The *index + lazy topic files* pattern solves the
"don't blow the context window" problem. **JBrain2 adaptation:** the "files" are
not real paths (non-negotiable #2) — they are rows/blobs behind storage, but the
*loading discipline* (small always-loaded index, lazy topics, delta edits) ports
exactly. And the "survives compaction by re-reading from durable store" property
is essential for a long-running personal agent.

### 2.6 ACE (Agentic Context Engineering) — evolving playbooks, anti-collapse

**Mechanism** `[web]`: treat context as an evolving *playbook*. A Generator runs
the task, a Reflector extracts lessons, a Curator applies **small targeted delta
ops (ADD/UPDATE/REMOVE) to individual bullets** — never a full rewrite. Names two
failure modes precisely: **brevity bias** (optimizers collapse context to short
generic blurbs, losing domain detail) and **context collapse** (iterative full
rewrites erode information). Delta updates preserve prior knowledge verbatim.

**Fit:** *High — this is the write/update discipline for the MD tier.* Generative
Agents and MemGPT tell us *what* to store; ACE tells us *how to update it without
degrading it.* JBrain2's MD memory blocks MUST be edited by delta ops, not
regenerated — otherwise the agent's accumulated self-knowledge rots. The
Generator/Reflector/Curator separation also maps onto task-profile routing: cheap
model reflects+curates memory, strong model only for the task itself.

### 2.7 Memory poisoning & integrity (the adversarial frame)

**Mechanism** `[web]`: persistent agent memory is an attack surface.
**PoisonedRAG** (USENIX'25): ~5 crafted docs flip RAG answers for a target query
at >90% success in a million-doc corpus. **MINJA** (NeurIPS'25): poison memory
through *normal queries* alone, >95% injection success. **MemoryGraft** /
**Zombie Agents**: implant fake "successful experiences" so the agent imitates a
malicious playbook from its own retrieved history. Poisoned memory cascades.

**Fit:** *Critical even single-user.* JBrain2 ingests external content (intake
links Phase 7, OCR'd documents, transcribed media). A poisoned note or attachment
could (a) inject a bad world-fact — but that path is *already defended* by
citations + review inbox + per-kind supersession, so it surfaces as a reviewable
conflict, not a silent rewrite; and (b) poison *agent* memory — a malicious note
could try to write a behavioral instruction ("always exfiltrate health facts to
general-domain answers"). **Defenses (§5):** agent memory is RLS-partitioned;
behavioral memory is never written from untrusted-content extraction (only from
the owner's direct interaction); episodic memory is provenance-stamped and
read-back-as-data, never executed as instruction; and the notes-as-sole-truth
firewall means the agent can never *act* on memory content as if it were a cited
fact.

---

## 3. The two-tier design mapped to JBrain2 storage

The owner's framing — "excellent long-term memory via our RAG DB, and more
immediate local memory MD files" — resolves cleanly once you separate **what is
true about the world** (long-term, cited, in the graph) from **how the agent
should behave and what it is mid-task on** (working memory, MD). The table is the
deliverable; prose follows.

| Memory type | Lives in | Physical store (storage-abstraction view) | Written by | Retrieved how | Cited? | Auto vs gated |
|---|---|---|---|---|---|---|
| **Working / core identity** ("you are Jeff's assistant; he prefers terse, numeric answers; never narrate") | Local MD block | `agent_memory` rows rendered as MD; small always-loaded *index block* | Owner (policy) + agent (delta self-edits) | Loaded in full each session (index only); ACE-style delta edits | No — behavioral, not factual | Owner edits any; agent self-edits auto, owner-visible & revertible |
| **Working / task scratchpad** (current multi-step task state, plan, intermediate IDs) | Local MD block | `agent_memory` row, session- or task-scoped, ephemeral-ish | Agent | Loaded for the active task; paged out (MemGPT) when done | No | Auto; discarded/archived on task completion |
| **Semantic (self)** — distilled behavioral learnings ("owner rejects wiki edits phrased as commands"; "for lab questions, retrieve `health` first") | Local MD topic file | `agent_memory` topic blocks, lazy-loaded by index | Agent (Reflector→Curator), seeded by owner corrections | Lazy-load by relevance; or promoted into index when hot | No | Agent auto-distills; **owner-visible diff**; consequential changes flagged |
| **Episodic** — conversation/task traces, tool-call logs, what was retrieved & decided | Postgres rows + memory-namespace embeddings | `agent_episodes` table (+ chunk/embedding rows in a *segregated namespace*); links to entity/fact IDs (A-MEM-style) | Agent (auto-append) | RRF hybrid search over the **memory namespace**, scored with recency+importance+relevance | No — provenance for the agent, never a wiki source | Auto-write; decay/compaction nightly; never gated (it's a log) |
| **Semantic (world)** — facts about the owner's life | **NOT agent memory** — the fact graph | `facts`/`entities`/`temporal_tokens` rows, cited to chunks | The extraction pipeline, from **notes** | RRF hybrid search over the knowledge corpus | **Yes — every fact cites a chunk** | Pipeline auto + review inbox; owner gates conflicts |
| **Prose knowledge** — synthesized articles | **NOT agent memory** — the wiki | `articles`/`revisions`/`citations` rows | Machine-only wiki builder | Wiki index hybrid search | Yes — citation FKs | Auto build; split/merge gated |
| **Procedural** — skills/playbooks | **Sibling researcher's lane** | (their call: skill files behind storage) | — | — | — | — |

### 3.1 What physically lives where, and the no-raw-paths constraint

There is no `/home/user/.../MEMORY.md` on a disk the agent opens by path. The
"MD files" are a **presentation format over storage-abstraction-backed rows**:

- A single `agent_memory` table (or content-addressed blobs keyed by a logical
  name + owner + domain), each row carrying `block_kind` (`core | task |
  self_semantic`), `domain_id`, `body_md`, `revision`, `updated_at`, and an
  append-only revision trail (mirroring the fact graph's "nothing is overwritten"
  doctrine — a behavioral preference change is a *transition with history*, not a
  blind overwrite).
- The **storage abstraction** owns whether that's a Postgres `text` column, a
  blob on the disk volume, or MinIO later — callers never see a path. This honors
  non-negotiable #2 exactly as attachments do today (sha256 blobs behind the
  abstraction).
- The agent reads/writes these via **tools** (`memory.read_index`,
  `memory.read_topic`, `memory.edit_block` with delta semantics), which run on an
  **RLS-scoped session** (non-negotiable #3) — the same plumbing the existing
  agent tools use (ARCHITECTURE.md "Agent").

### 3.2 Why episodic memory is rows + a *segregated* embedding namespace

Episodic traces are reused for retrieval ("last week you told me the dentist
moved — what did we conclude?"), so they need embeddings and FTS. We reuse the
*exact* pipeline (chunk → embed via the `embed` container → tsvector → RRF) — "one
Postgres does everything" extends naturally. The **non-negotiable twist**: these
embeddings sit in a *memory namespace* (a discriminator column / partition the
hybrid-search query filters on), so that:
- agent queries can search memory + graph together when helpful, but
- **wiki builds and fact-citation retrieval search only the knowledge corpus** —
  an episodic trace can never be matched as a citable fact. Segregation is a
  query-time filter *and* an RLS-policy-eligible column, so it's enforceable, not
  conventional.

### 3.3 Retrieval scoring: RRF + recency + importance

The existing RRF fuses dense + FTS by rank. For memory namespaces, extend the
fusion with two Generative-Agents-derived signals `[web]`:
- **recency**: exponential decay on `last_accessed_at` (cheap, in SQL).
- **importance**: an LLM "poignancy" score assigned at episode-write time (a
  cheap-tier task profile), or derived heuristically (did the owner correct the
  agent? tool error? explicit "remember this"?).
These become additional ranked lists folded into RRF — no new retrieval engine,
just more input rankings. World-fact retrieval keeps pure-relevance RRF (a fact's
truth doesn't decay with recency; its *validity interval* is already modeled
bitemporally in ANALYSIS.md).

### 3.4 The procedural seam (sibling researcher owns this)

Procedural memory = skills/playbooks the agent executes. That is a separate
dossier. The **seam this dossier guarantees**: (a) skills, when they run, *read*
working + episodic memory and *write* episodic traces through the same tools; (b)
a skill that "learns" a better procedure updates *its own* skill definition (their
mechanism), not the world-fact graph; (c) if a skill discovers a world-fact, it
files a correction note like everyone else. The contract is: **memory provides
state and self-knowledge; skills provide behavior; neither becomes a source of
truth.**

---

## 4. Reconciliation: agent memory vs notes-as-sole-truth (critical)

This is the central tension and it must be addressed head-on, because the naive
agent-memory designs in the literature (MemGPT archival, A-MEM, Generative-Agents
reflection) are all **shadow-truth machines**: they let an agent distill content
into an un-cited private store and then *act on it as fact*. JBrain2 forbids this
by constitution (non-negotiable #7: the wiki is machine-written from notes;
humans correct via notes, never direct edits; ANALYSIS.md: "notes are the sole
sources of truth").

### 4.1 The bright line

> **Agent memory may remember *how the agent thinks and behaves*. It may NOT
> remember *what is true about the owner's life* as an independent, citable
> assertion.**

Concretely:
- ✅ Legitimate agent memory: "Jeff prefers I answer health questions with the
  raw lab number first." (a behavioral preference — about the *interaction*)
- ❌ Forbidden agent memory: "Jeff's cholesterol is 210." (a world-fact — must be
  a `measurement` fact extracted from a note, cited to a chunk, retrieved live)

The test: *if the statement would belong in the wiki, it may not live in agent
memory.* World-facts have exactly one home — the cited graph.

### 4.2 Why this isn't merely a rule but is structurally enforced

Three mechanisms make the bright line load-bearing rather than aspirational:

1. **Segregated namespace (3.2):** agent memory embeddings are query-filtered out
   of fact-citation and wiki retrieval. The wiki builder *cannot* cite an agent
   memory because its retrieval never sees the namespace. A citation is an FK to a
   `fact`/`chunk` row (ARCHITECTURE.md "Wiki") — agent memory rows are not in
   those tables, so a citation to one is a foreign-key impossibility, not a
   policy someone might forget.

2. **The one sanctioned promotion path is the correction note.** When the agent
   *does* infer something about the world worth persisting ("the owner mentioned
   in chat his address changed"), it does NOT write a fact. It drafts a
   **correction note** / ordinary note that flows through normal ingestion →
   extraction → facts-with-citations → review inbox. The agent's inference becomes
   ground truth only after passing the same provenance gate as every other note.
   This is the *exact* mechanism the architecture already uses for the wiki
   correction loop (ARCHITECTURE.md) — we reuse it, we don't invent a backdoor.
   The chat-derived note cites the conversation as its source, so even
   agent-originated facts trace to a real artifact.

3. **Pointers, not copies.** When agent memory needs to *refer* to a world-fact
   (e.g., a task scratchpad tracking "we're resolving the dentist-reschedule"),
   it stores the **fact/entity/temporal-token ID**, not the fact's content. On
   read, the agent re-fetches the live fact through RLS — so it always sees the
   *current* value (post-supersession), and it always has the citation. A
   superseded address in a stale memory copy is impossible because there is no
   copy. (Superseded facts stay queryable for citation integrity — ARCHITECTURE.md
   — so even pointers to old facts resolve.)

### 4.3 Staleness and conflict — solved by *not* duplicating

Most agent-memory staleness bugs (the memory says X, the world now says Y) simply
*cannot occur* for world-facts, because agent memory holds no world-facts — only
IDs that resolve to the live graph, where supersession is already the law
(ANALYSIS.md per-kind policy). The graph's bitemporal model, `superseded_by`
chains, and newest-wins-with-review handle conflict; agent memory inherits all of
it for free by referencing rather than copying.

Staleness *can* still affect the legitimate residents of agent memory —
behavioral preferences. "Jeff likes terse answers" might stop being true. Handle
this with the graph's own doctrine, scaled down: preference blocks are
**append-only with a current binding** (a preference change supersedes the old
binding, old one stays agent-visible — mirroring the `preference` fact kind in
ANALYSIS.md, which already says superseded preferences stay agent-visible). And
because these are MD blocks the owner can read via a "what do you remember about
me?" view, correction is direct and cheap.

### 4.4 The summary doctrine

JBrain2 already *has* the two "reflection/summarization" engines the agent-memory
literature reinvents — the **fact extractor** (raw text → structured cited facts)
and the **wiki builder** (facts → synthesized cited prose). The self-improving
agent does not get a *third*, un-cited summarizer for world-knowledge. Its only
"summarization" privilege is over its *own* episodic stream and *own* behavior.
This is the cleanest possible reconciliation: **the agent improves how it works;
the pipeline owns what is true.**

---

## 5. RLS / domain scoping of memory

Agent memory is subject to the *same* firewall as everything else (ARCHITECTURE.md
"subjects, principals, domains"; non-negotiable #3). Memory must never become the
leak vector that the rest of the system carefully prevents.

### 5.1 Every memory row carries a `domain_id`, every query is RLS-scoped

`agent_memory` and `agent_episodes` (and their chunk/embedding rows) get a
`domain_id` column and the standard `has_domain_scope` RLS policy, with an RLS
isolation test proving a scoped session cannot read another domain's memory
(non-negotiable #3 + #5: every new table needs an RLS isolation test). The agent's
memory tools run on the session's domain-scope GUC — a `health`-scoped chat
retrieves only `health` episodic memory.

### 5.2 The hard case: cross-domain behavioral memory

Behavioral preferences feel domain-general ("Jeff likes terse answers"), but some
are domain-*specific and sensitive* ("for finance questions, Jeff wants me to
flag anything over $X"). Decision:
- **Core identity / general behavioral blocks** are `general`-domain (the owner's
  sessions carry all scopes, so they're always loaded for the owner).
- **Any behavioral block derived from a sensitive-domain interaction is written
  into that domain**, not `general`. The asymmetric-classification rule from
  ANALYSIS.md applies verbatim: misclassifying a preference *into* health/finance
  is cheap; *out of* it is a leak. A preference learned during a health chat
  defaults to `health` unless it's provably generic.

### 5.3 Episodic memory is the sharpest leak risk

An episodic trace of a `health` conversation contains health content (what was
retrieved, what the agent said). If that trace were retrievable in a `general`
session, it would leak. Mitigation, layered:
1. **Domain-stamp at write:** an episode inherits the domain-scope of the session
   that produced it. A mixed-domain conversation is **split into per-domain
   episode rows** (the same "mixed notes → per-domain derived chunks" trick from
   ANALYSIS.md) so no single episodic chunk straddles the firewall.
2. **RLS at read:** retrieval is domain-scoped; a `general` session never sees
   `health` episodes.
3. **Citation/cross-subject safety:** episodes link to fact/entity IDs;
   fact→subject attribution is a security field (ANALYSIS.md — cross-subject
   misattribution is a leak). An episode that pointed at another subject's fact
   would be caught by the same RLS that guards the fact.

### 5.4 Poisoning resistance, RLS-flavored

From §2.7 `[web]`: the defenses are (a) **behavioral memory is never written from
untrusted-content extraction** — only the owner's direct, authenticated
interactions can create/modify behavioral blocks; a malicious *note* can propose a
world-fact (defended by review inbox) but can never silently install an agent
instruction; (b) **episodic memory is read back as data, never as instruction** —
the agent treats its trace as "here is what happened," not "here is what to do,"
blunting MemoryGraft-style "imitate your past success" attacks; (c)
**namespace + RLS segregation** means a poisoned health note can't surface in a
general answer even if extraction is fooled; (d) **owner visibility** — the
"what do you remember?" view makes silent poisoning of behavioral memory
auditable, the same way the review inbox makes fact poisoning auditable.

### 5.5 Forgetting / decay under RLS

Forgetting is a feature, scoped:
- **Episodic decay (auto):** nightly job (the existing scheduler) ages out
  low-importance, low-recency episodes — either hard-delete or compact a cluster
  into one distilled *self*-semantic note (NOT a world-fact). Compaction runs
  per-domain under RLS so a compaction job can't merge across the firewall.
- **Behavioral memory: never auto-forgotten** — only superseded by a newer
  binding or owner edit (it's small, high-value, and silent loss would be a
  regression). Mirrors `attribute`-kind "never auto-supersede" caution.
- **Note deletion cascades to memory:** ANALYSIS.md is absolute — deleting a note
  purges every derived artifact. Episodic memory that *quotes or embeds* deleted
  note content must be caught by the same purge (another reason episodes store
  *pointers*, not copies: a pointer to a purged fact is repaired/dropped by the
  existing chain-repair sweep, no orphaned health content lingering in a memory
  blob).

---

## 6. Sources

| # | Source | Claim(s) it supports | Type |
|---|---|---|---|
| 1 | MemGPT/Letta — tiered memory, self-editing blocks, paging — https://arxiv.org/pdf/2310.08560 ; https://www.leoniemonigatti.com/blog/memgpt.html ; https://sureprompts.com/blog/letta-memgpt-walkthrough | §1.3, §2.1, §3 working-memory paging | `[web]` |
| 2 | Generative Agents (Park et al.) — memory stream, recency/importance/relevance retrieval, reflection — https://arxiv.org/pdf/2304.03442 ; https://ar5iv.labs.arxiv.org/html/2304.03442 | §2.2, §3.3 retrieval scoring | `[web]` |
| 3 | A-MEM: Agentic Memory for LLM Agents (NeurIPS 2025) — Zettelkasten links, self-evolving notes — https://arxiv.org/abs/2502.12110 ; https://github.com/agiresearch/a-mem | §2.3, §3.2 link-on-write | `[web]` |
| 4 | "Memory in the Age of AI Agents" survey / MIRIX typology; episodic-collapse warning — https://www.preprints.org/manuscript/202601.0618 ; https://arxiv.org/html/2603.07670v1 ; https://github.com/Shichun-Liu/Agent-Memory-Paper-List | §2.4 typology; keep episodic raw | `[web]` |
| 5 | Claude Code memory docs — CLAUDE.md vs auto-memory, MEMORY.md index + lazy topic files, 200-line/25KB load, survives `/compact` — https://code.claude.com/docs/en/memory | §2.5, §3 MD-tier loading discipline | `[web]` |
| 6 | ACE: Agentic Context Engineering (ICLR 2026) — playbook delta ADD/UPDATE/REMOVE, brevity bias, context collapse — https://arxiv.org/abs/2510.04618 ; https://arxiv.org/html/2510.04618v1 | §1.3, §2.6, §3 delta self-edits | `[web]` |
| 7 | Memory poisoning: PoisonedRAG (USENIX'25), MINJA (NeurIPS'25), MemoryGraft, Zombie Agents — https://arxiv.org/abs/2512.16962 ; https://arxiv.org/html/2503.03704v2 ; https://workos.com/blog/ai-agent-memory-poisoning | §2.7, §5.4 poisoning resistance | `[web]` |
| 8 | JBrain2 ARCHITECTURE.md (hybrid RRF search, facts/supersession, domains/RLS, storage abstraction, wiki citations, correction loop, agent tools) | §3, §4, §5 — JBrain2 grounding | `[training]` (repo doc) |
| 9 | JBrain2 ANALYSIS.md (fact kinds & per-kind supersession, bitemporal model, preference kind agent-visibility, mixed-domain split, note-deletion purge, asymmetric domain classification, cross-subject = leak) | §4.3, §5.2–5.5 | `[training]` (repo doc) |
| 10 | JBrain2 CLAUDE.md (non-negotiables: LLM adapter, storage abstraction, RLS sessions + isolation tests, machine-written wiki / correction notes) | §1, §3.1, §4, §5.1 | `[training]` (repo doc) |

**Note on labels:** All external paradigm mechanisms are `[web]` (verified
post-cutoff against the cited URLs, June 2026). All JBrain2 mappings rest on the
three repo docs (`[training]`-context, i.e. read directly from the repository).
The synthesis — which paradigm maps to which JBrain2 store, and the
notes-as-sole-truth reconciliation — is this dossier's contribution.
