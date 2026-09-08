# X3 — Prior art: conversational/agentic knowledge-graph construction

> **Status:** Research · **Last verified:** 2026-09-08

Scope: the external landscape for the proposed change — replacing JBrain2's
deterministic `extract → integrate → arbiter → apply` pipeline with a
**conversational agent** that owns the entity/predicate graph through tools,
commits what it is confident about, and asks the owner about the rest. Notes stay
the sole source of truth; inference is local-only (gpt-oss-120b + a Qwen3 vision
model, one 128 GB box).

**How to read the evidence.** Three labels are used throughout:

- **[verified]** — a primary source (paper, model card, code/issue tracker, docs)
  states it, and I read the statement.
- **[vendor]** — the claim comes from the party selling or promoting the thing.
  Treated as a hypothesis, not a fact. Several are actively disputed (§1.7).
- **[secondary]** — a third-party blog/analysis with no primary source attached.
  Directionally useful, not load-bearing.

A recurring theme: **this whole subfield's benchmark numbers are unreliable**
(§1.7). Design decisions below are anchored on mechanism and postmortems, not on
leaderboard deltas.

---

## 1. The landscape

### 1.1 Zep / Graphiti — the closest thing to this repo's design

Graphiti is a temporal knowledge-graph engine; Zep is the hosted memory service
built on it. It is the nearest prior art to JBrain2's supersession chains.

**How facts get created** [verified,
[arXiv:2501.13956](https://ar5iv.labs.arxiv.org/html/2501.13956)]: each "episode"
(a message or document) runs a fixed **six-stage pipeline** — entity extraction
(current message + prior *n*=4 messages, with a reflexion-style self-check),
entity resolution/dedup (embedding cosine + full-text hybrid search, then an LLM
compares candidates), edge/fact extraction, edge dedup (hybrid search *constrained
to edges between the same entity pair*), temporal extraction (absolute + relative
timestamps resolved against a reference time), and edge invalidation.

**Temporal model** [verified, same source]: four timestamps per edge —
`t'_created` / `t'_expired` on the transaction timeline, `t_valid` / `t_invalid`
on the event timeline. Contradiction handling is: *an LLM compares the new edge
against semantically related existing edges; on a temporally-overlapping
contradiction, the old edge is invalidated (a `t_invalid` is written), never
deleted.* Zep's docs frame this as "temporal edge invalidation rather than
LLM-driven judgment calls" [vendor,
[getzep overview](https://help.getzep.com/graphiti/getting-started/overview)] —
which is marketing gloss: per the paper the *detection* step is an LLM call; only
the *write* is mechanical.

**Human in the loop:** none. Graphiti/Zep has no review queue, no confirmation
step, no correction channel other than ingesting more text. This is the single
biggest structural difference from what JBrain2 is proposing.

**What they learned the hard way** [vendor but mechanistic and credible,
[Zep engineering blog](https://blog.getzep.com/llm-rag-knowledge-graphs-faster-and-more-dynamic/)]:
their v1 was a **single "mega-prompt"** that extracted entities and facts together
with large amounts of graph context injected. It "couldn't scale as graphs grew
larger," was slow, and "caused frequent hallucinations, especially with smaller
LLMs." The fix was **separation of concerns** — split into the six narrow tasks
above — plus **capping the context of the dedup prompt** by retrieving only the
top-k similar nodes via hybrid search, so the prompt "won't indefinitely scale
linearly with graph size." Their framing is worth quoting: *"LLMs provide output
used to build our database, rather than text output for human consumption,"*
therefore consistency beats eloquence.

**What they got wrong / what still hurts** [verified, issue tracker]: cost and
LLM-call count per ingest. [Issue
#1193](https://github.com/getzep/graphiti/issues/1193) — "a single activity can
trigger multiple LLM calls and embedding calls (node extraction, edge extraction,
deduplication, etc.), and at scale this becomes very expensive"; the requester
wants to bring their own extraction and bypass the internal LLM entirely. [Issue
#1299](https://github.com/getzep/graphiti/issues/1299) asks for `add_episode`
*without* LLM extraction at all. Zep's later mitigation was to add deterministic
classical-IR front-ends and fall back to the LLM only when needed [secondary,
[Medium](https://akkonrad.medium.com/knowledge-graphs-arent-databases-anymore-they-re-the-memory-layer-for-ai-agents-d090c03eb58c)].

**Ontology control:** Graphiti supports Pydantic-defined custom entity/edge types
and an `excluded_entity_types` parameter to suppress extraction of types you don't
want [verified,
[Zep docs](https://help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types)].
Notably there is an open request to make *entity* typing free-form the way edges
already are ([issue
#1308](https://github.com/getzep/graphiti/issues/1308)) — the same tension
JBrain2's two-tier predicate model resolves in the opposite direction.

### 1.2 mem0 — the system that *retreated* from LLM-driven updates

mem0's original algorithm was two LLM passes: pass 1 extracted candidate facts;
pass 2 reconciled them against existing memory with **ADD / UPDATE / DELETE /
NOOP** operations [verified,
[mem0 blog](https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm)].

**They removed the reconciliation pass.** The current algorithm is a **single,
add-only** LLM call: every extracted fact becomes an independent record, and when
information changes the new fact simply lives alongside the old one. Their stated
reasons [verified, same source]:

- "Overwrites sometimes erased key information from the original fact."
- "Deletes sometimes removed information that would be relevant later."
- Reconciliation was "where context got destroyed."
- Extraction latency roughly halved once the pass was dropped.

This is the most directly transferable negative result in the entire landscape:
**a team that shipped LLM-decided UPDATE/DELETE against a live memory store took
it back out**, and cited silent information loss as the reason. JBrain2's
supersession-with-retained-history is *already* the safer form of this (nothing is
overwritten; the old head stays in the chain). The lesson is not "don't
supersede"; it's "don't let the model *delete*, and don't make the model diff
against existing state in the same breath as it reads the note."

### 1.3 Letta / MemGPT — self-editing memory, and why they split the agent

MemGPT introduced agent-editable in-context **memory blocks** (a `human` block and
a `persona` block, each character-capped) that the agent rewrites with tool calls
[verified, [Letta blog](https://www.letta.com/blog/memory-blocks/)].

The important later admission [verified,
[Letta sleep-time compute](https://www.letta.com/blog/sleep-time-compute/)]:
bundling memory management, conversation, and task tools into **one** agent made
it "potentially less reliable since it calls both memory management tools and
standard tools." Their fix — **sleep-time compute** — moves memory maintenance to
a *separate asynchronous agent* that rewrites shared memory blocks during idle
time, improving both response time and memory quality. The general research case
for the pattern is in [arXiv:2504.13171](https://arxiv.org/pdf/2504.13171).

The acknowledged weakness of self-editing memory: *"memory quality depends
entirely on the model's judgment, and if the model fails to save something, it's
gone"* [secondary, Letta-adjacent write-ups]. There is no retry and no audit — the
failure is silent.

**Relevance to the proposal:** the design under consideration is exactly the
bundled-agent shape Letta backed away from — one conversation that both talks to
the owner and mutates the graph. Letta's answer was to split the roles, not to
make one agent better at both.

### 1.4 Cognee — ontology validation as a hallucination gate

Cognee runs an ECL (Extract → Cognify → Load) pipeline; `cognify` classifies
documents, chunks, extracts graph triplets with an LLM, summarizes, and commits to
vector + graph stores [secondary,
[cognee docs/blog](https://www.cognee.ai/blog/deep-dives/grounding-ai-memory)].

The interesting mechanism: **ontology-based entity validation**. An OWL/RDF
resolver checks LLM-extracted entities against a supplied ontology and stamps each
node with an `ontology_valid` flag, so grounded entities are distinguishable from
hallucinated ones; for matched entities the LLM-derived name is *replaced by the
canonical ontology URI-derived name*, which kills cross-document duplicates at the
source [secondary, same]. It then BFS-traverses the surrounding ontology and
injects those relationships.

That `ontology_valid` flag is a cheap idea worth stealing: **do not reject
unknown, but do mark known-ness on the row** — which is essentially what JBrain2's
`declares_predicate` tier flag already is. Cognee's addition is that the flag is
carried on the *instance* and is queryable, so "show me everything the model coined
on its own" is one predicate away.

### 1.5 Basic Memory — files are the graph

Basic Memory stores one Markdown file per entity, containing a title, frontmatter,
`observations` (individual facts) and `relations` (links to other entities); a
local SQLite index makes it traversable, and **the Markdown files are the source of
truth** [verified,
[GitHub](https://github.com/basicmachines-co/basic-memory)]. The vault opens
directly in Obsidian, so the human edits the same artifact the LLM writes.

This is the philosophical opposite of JBrain2's "the wiki is machine-written only;
humans correct via correction notes." Basic Memory is popular *because* the human
can just fix the file. The cost is that there is no provenance, no supersession
history, and no way to tell a machine assertion from a human one — exactly the
properties JBrain2 chose to buy with its stricter rule. Worth knowing the trade is
deliberate, and worth noting that the market's revealed preference is for direct
editability.

### 1.6 LlamaIndex property graph index — the strict/loose schema dial

`SchemaLLMPathExtractor` extracts paths against a declared schema of
`possible_entities`, `possible_relations`, and a `kg_validation_schema` saying
which entities may carry which relations, using Pydantic + structured outputs
[verified,
[LlamaIndex docs](https://developers.llamaindex.ai/python/framework/module_guides/indexing/lpg_index_guide/)].
The `strict` flag is the whole design in one parameter: `strict=True` rejects
out-of-schema triples; `strict=False` treats the schema as a *suggestion*.

LlamaIndex's own comparison of extractors concludes the strict-schema graph "should
be the most consistent but might miss a lot of relationships that don't fit the
predefined schema" [verified,
[dynamic KG extraction notebook](https://developers.llamaindex.ai/python/examples/property_graph/dynamic_kg_extraction/)].
JBrain2's two-tier model is `strict=False` **plus a tier flag** — strictly better
than either pole, and it is worth noting that the mainstream framework offers only
the binary.

### 1.7 Microsoft GraphRAG — the cost cliff, and the retreat to lazy construction

GraphRAG's indexer LLM-extracts entities and relations per text unit, then runs
extra "gleaning" passes to catch what the first pass missed, then builds
hierarchical community summaries [verified,
[GraphRAG methods docs](https://microsoft.github.io/graphrag/index/methods/)].

**Graph extraction is roughly 75% of indexing cost**, and few-shot extraction
prompting inflates token usage 3–5× [secondary,
[Graph Praxis](https://medium.com/graph-praxis/the-graphrag-cost-cliff-how-33-000-became-33-in-eighteen-months-be1b0fbe37e4);
[premai guide](https://blog.premai.io/graphrag-implementation-guide-entity-extraction-query-routing-when-it-beats-vector-rag-2026/)].
Microsoft's own guidance warns indexing can be costly and recommends starting
small, and they shipped `--estimate-cost` so you can see the bill before paying
it.

The response was **LazyGraphRAG**: defer expensive graph construction to query
time. That is the strategic point for JBrain2 — the flagship "LLM builds a
knowledge graph from your corpus" project's headline lesson after two years is
*build less graph, later*.

### 1.8 AriGraph — episodic + semantic, and why the split matters

AriGraph (IJCAI 2025, [arXiv:2407.04363](https://arxiv.org/abs/2407.04363))
maintains **two coupled memories**: episodic vertices holding the *full textual
observation*, and a semantic triplet graph extracted from it, with episodic edges
linking each observation to the triplets it produced. The agent outperformed
full-history, summarization, and RAG baselines on TextWorld tasks and reached
near-human performance on text games.

The architecture is the citation for **"keep the raw text as a first-class node,
link every derived triple back to it."** JBrain2 already does this via
`note_id`/`chunk_id` provenance; AriGraph is evidence that keeping the episodic
side *retrievable in its own right* (not just as a citation) is what makes the
semantic side safe to be lossy.

### 1.9 Obsidian / Logseq AI plugins — what personal-KG users actually tolerate

Obsidian's Smart Connections runs a **local** embedding model and surfaces
semantically related notes in a sidebar; it does not write links [verified,
[plugin repo](https://github.com/brianpetro/obsidian-smart-connections)]. The
community guidance for auto-linking plugins is blunt: *review suggestions, don't
just accept them — auto-linked notes that don't actually belong together create
noise that's worse than no links at all*, and *if your tag system is already
inconsistent, the plugin will inherit that inconsistency and amplify it*
[secondary,
[Medium](https://medium.com/@theo-james/automating-tagging-and-linking-with-ai-plugins-1a76bda05637)].

Note what survived in this market: **suggest-only, local, read-only-to-the-vault**.
The write-heavy plugins are not the popular ones.

### 1.10 Rewind / Personal.ai class — the durability lesson

Rewind's Mac app stopped capturing on **2025-12-19** after Limitless was acquired
by Meta; there was no in-app migration path and the screen-recording archives were
never designed to be exported [secondary, but consistent across sources:
[rewind.ai's own notice](https://rewind.ai/what-happened-to-rewind/),
[littlebird summary](https://littlebird.ai/blog/rewind-alternatives)]. Meta wanted
the wearables team, not the recorder.

For a self-hosted, local-only system this is mostly a "you already did the right
thing" datapoint — but the sharper version is: *the memory must remain readable
without the agent that made it.* If the graph is only interpretable by the
conversational agent that wrote it, you've reinvented the un-exportable archive
inside your own box.

### 1.11 The benchmark integrity problem (read before believing any number above)

- Zep published **84%** on LoCoMo. mem0 re-ran it and got **58.44% ± 0.20**,
  citing three method errors: inclusion of the adversarial 5th category that the
  benchmark protocol excludes, a system-prompt/retrieval-template configuration
  applied to Zep but not to baselines, and a single run reported without variance
  [verified, [zep-papers issue
  #5](https://github.com/getzep/zep-papers/issues/5)]. Zep replied that it was
  misconfigured and claimed 75.14% [secondary]. **Nobody can reproduce either**,
  because the contested choices aren't prescribed by the benchmark.
- Zep in turn published "Is Mem0 Really SOTA in Agent Memory?" [vendor,
  [blog](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/)].
- An independent audit of LoCoMo found **6.4% of the answer key is wrong**
  (~99 wrong/hallucinated/misattributed/ambiguous answers across all ten
  conversations), the **LLM judge accepts 63% of intentionally wrong answers**, and
  **56% of per-category system comparisons are statistically indistinguishable from
  noise** [secondary but methodologically explicit,
  [Penfield Labs](https://penfieldlabs.substack.com/p/proposal-a-new-benchmark-for-long)].
  It also notes LoCoMo conversations are only ~16–26k tokens — inside a modern
  context window, so the benchmark barely tests long-term memory at all.
- mem0's own published results show a plain **full-context baseline beating their
  system** (~73% vs ~68% LLM-judge) [secondary, same audit].

**Consequence for this project: do not justify the agentic rewrite with any
published memory-system benchmark.** They do not survive contact with replication.
The justification has to be a mechanism argument plus a local eval harness.

---

## 2. Human-in-the-loop: when is asking worth the interruption?

This is the part of the proposal with the best available literature, and it is
mostly *cautionary about asking too much*.

### 2.1 The value-of-information framing is now formalized

**SAGE-Agent** [verified, [arXiv:2511.08798](https://arxiv.org/abs/2511.08798)]
maintains an explicit probabilistic belief over structured *tool-call candidates*
(probabilities factored per parameter; specified parameters get 1.0, unspecified
finite-domain parameters get a uniform prior), separates **specification
uncertainty** (what the user wants) from **model uncertainty** (LLM limits), and
scores each candidate question by **Expected Value of Perfect Information** minus a
redundancy cost.

The stopping rule is the piece worth copying verbatim:

> ask only while `max_q [ EVPI(q) − Cost(q,t) ] ≥ α · max_c π_c(t)` — with α ≈ 0.1

i.e. **stop asking as soon as the best remaining question is worth less than a tenth
of your current best guess's confidence.** Results on ClarifyBench (716 samples,
5 domains): coverage 59.73% vs 55.70% for a domain-aware ReAct baseline, at **1.39
questions per task vs 2.56 — 1.8× fewer questions** [verified]. Consistent across
GPT-4o and Qwen2.5-14B, which matters here: the mechanism is not frontier-only.

### 2.2 Over-asking is measurably as bad as under-asking

**"Ask or Assume?"** [verified,
[arXiv:2603.26233](https://arxiv.org/abs/2603.26233)] instruments coding agents on
underspecified tasks:

- Autonomous (never ask): **54.80%** resolution.
- Forced-interactive (ask nearly every time): **70.40%** — but it queried "in
  nearly every instance," emitting 251-token questions and eliciting 415-token
  answers even when unnecessary.
- Uncertainty-aware: comparable resolution, **76.92% of tasks resolved with zero
  user interaction**, questions concentrated where difficulty warranted them
  (9.28% higher query rate on medium vs easy tasks).
- The scaffold **doubled inference cost** ($3.50 vs $1.63/task).
- Recommended budget: **≤3 interactions per task**; successful runs averaged 3.06
  queries when they interacted at all.

Note the shape: the forced-asking baseline was *not much worse at the task*. Its
cost was **entirely friction**. In a personal system where the "user" is one person
who also has a day job, friction *is* the failure mode.

### 2.3 Timing beats volume

**"Ask Early, Ask Late, Ask Right"** [verified,
[arXiv:2605.07937](https://arxiv.org/html/2605.07937v1)] — 6,000+ runs, 3
benchmarks, 4 frontier models, injecting ground-truth clarifications at 10/30/50/
70/90% of the trajectory:

- **Goal** clarification has a **narrow early window (≤10% of execution)**:
  0.78 pass@3 at 10%, decaying to the 0.40 no-clarification baseline by 70%.
- **Input** clarification decays gradually and still pays through ~50%.
- **Constraint** clarification is nearly worthless at any point.
- Wasted compute grows **0% → 21.7%** as clarification is delayed to 90%.
- Models ask badly on their own: GPT-5.2 asks 43% of the time and misses the goal
  window; Gemini never asks. Timing effects are task-intrinsic (Kendall's τ
  0.78–0.87 across models), not model-specific.

Translated to note ingest: **an ambiguity about *what the note is about* must be
raised at the top of the conversation or not at all**; an ambiguity about a
*specific value* can wait and be batched.

### 2.4 The fatigue evidence from a field that already lost this fight

Clinical decision support is the best-quantified natural experiment in
interruptive review queues:

- Alert **override rates range 46.2%–96.2%** across studies [verified,
  [systematic review
  PDF](https://www.metrohealth.org/globalassets/metrohealth-documents/population-health-research-institute/ray-wilson-et-al-2026-alert-fatigue-systematic-review.pdf)].
- A three-year Brigham & Women's study: clinicians overrode **73.3%** of medication
  alerts, and **40% of those overrides were inappropriate** — i.e. real signal was
  dismissed because of the noise around it [secondary,
  [FDB summary](https://www.fdbhealth.com/insights/articles/2023-04-18-reduce-medication-alert-fatigue-with-patient-focused-clinical-decision-support)].
- **88.2% of alerts flagged "very severe" were still overridden** [verified,
  [JMIR Med Inform 2022](https://medinform.jmir.org/2022/10/e40511)].
- Mechanism: "alert fatigue is unavoidable when many irrelevant alerts are
  generated in response to a small number of useful alerts"; the recommended fix is
  **specificity**, not volume controls bolted on later [verified, same].

This is the empirical backing for what the refocus plan already observed about
`new_predicate` cards: *one open card per distinct raw spelling, review noise,
forever.* The CDS literature says the endgame of that is not "the owner works
through the backlog" — it's **the owner stops reading the queue, including the
items that mattered**, and cannot tell you which ones they missed.

### 2.5 Batching is cheap; sequential asking is not

Batch active learning is preferred in practice because "the cost of acquiring a
batch of labels might be significantly less than acquiring the same number of
sequential individual label requests," particularly when the model must update
between queries [verified,
[ACM WWW'18 batch AL for HITL relation extraction](https://dl.acm.org/doi/fullHtml/10.1145/3184558.3191546)].
The same literature warns that most deep-AL query-strategy work was validated only
in *simulation*, without measuring real annotation time or quality — so "the model
picked the most informative question" is not evidence that a human enjoyed
answering it.

---

## 3. Temporal / bi-temporal fact modeling

### 3.1 The standard vocabulary

- **Valid time** — when the fact held in the world. **Transaction time** — when the
  store learned it. Both together = bitemporal; this is 1980s temporal-database
  work, standardized in SQL:2011 as application-time vs system-versioned periods
  [verified,
  [Bitemporal Property Graphs, ADBIS](https://link.springer.com/content/pdf/10.1007/978-3-032-05281-0_15.pdf?pdf=inline+link)].
- Real KGs mostly pick one: **NELL is transaction-time** (every fact carries its
  extraction date); **YAGO and Wikidata are valid-time** (facts carry validity
  scope only) [verified,
  [Towards Probabilistic Bitemporal KGs, WWW'18](https://dl.acm.org/doi/fullHtml/10.1145/3184558.3191637)].
- **XTDB** is genuinely bitemporal (valid time is user-settable, so *retroactive*
  writes are first-class); **Datomic is unitemporal** — one system-time line per
  entity [verified,
  [XTDB docs](https://v1-docs.xtdb.com/concepts/bitemporality/),
  [XTDB FAQ](https://cljdoc.org/d/com.xtdb/xtdb-core/1.24.0/doc/faqs)].
- Recent RDF work (BiTRDF) makes *all* resources and relationships inherently
  bitemporal and models time as references rather than attributes
  [verified, [MDPI](https://www.mdpi.com/2227-7390/13/13/2109)].

### 3.2 Graphiti's model vs JBrain2's supersession chains

They are the same idea with different ergonomics.

| | Graphiti | JBrain2 today |
|---|---|---|
| Address | edge between two entity nodes | `entity.predicate[.qualifier]` |
| Transaction time | `t'_created` / `t'_expired` | fact row create + supersession link |
| Valid time | `t_valid` / `t_invalid` | `valid_from` / `valid_to` (SCD-2 for `state`) |
| Who decides a contradiction | an LLM, per-edge, at ingest | the arbiter, per-kind policy, deterministic |
| Retrospective ("the 2019 note") | LLM must notice | explicit rule: a retrospective note must not supersede current |
| History | invalidated edges retained | chain retained, chain repair on delete |

**The honest read: JBrain2's model is not behind Graphiti's — in two respects it is
ahead.** (a) The per-kind conflict policy (`event`/`measurement` never
auto-supersede; `attribute` collisions hold for review) encodes domain knowledge
Graphiti hands to an LLM every time. (b) Chain repair on note deletion is a
provenance guarantee Graphiti doesn't make. What Graphiti has that this repo does
not is a **uniform, queryable four-timestamp surface** — "as of transaction time T,
what did the system believe was valid at time V" is a single query there and an
awkward one here.

### 3.3 The one model worth adopting while the DB is disposable

**Name the four timestamps explicitly on the fact row.** Today `valid_from`/
`valid_to` plus the supersession link *encode* bitemporality; they don't
*expose* it. Adding `asserted_at` (transaction time in) and `retracted_at`
(transaction time out) as real columns — with the supersession chain kept as the
lineage pointer, not as the temporal source of truth — buys:

- point-in-time-of-belief replay ("what did the graph say last Tuesday"), which is
  the only honest way to debug an agent that writes the graph;
- retroactive correction with an audit trail (XTDB's valid-time-in-the-past write),
  which is exactly what a correction note *is*;
- a clean separation between "this was wrong" (retract: close transaction time,
  leave valid time alone) and "this stopped being true" (supersede: close valid
  time). **These are different events and the current model conflates them.**

That last distinction is the substantive finding. It is also what the
[TOKI](https://arxiv.org/pdf/2606.06240) paper attacks Graphiti and mem0 for
missing: no formal contradiction model, irreversible updates that cannot cleanly
retract a false belief without data loss, conflation of transaction and valid
time, and **"silent belief corruption" — users cannot detect that memory contains
contradictions** [verified from the paper's own framing; its formal results I have
not independently checked — treat the theorems as unverified, the failure taxonomy
as sound].

---

## 4. Local open-weight models driving tool-using agents

### 4.1 gpt-oss-120b — real numbers

From the OpenAI model card [verified,
[arXiv:2508.10925](https://arxiv.org/pdf/2508.10925)]:

| Benchmark | low reasoning | medium | high |
|---|---|---|---|
| τ-bench Retail | 49.4% | 62.0% | **67.8%** |
| τ-bench Airline | 42.6% | 48.6% | **49.2%** |

τ-bench is the relevant one — it is task-level completion across a *chain* of tool
calls with a simulated user, not single-call accuracy. **Read the airline number as
the ceiling for a long multi-turn tool conversation: roughly one run in two
completes.** Retail at high reasoning is better but still ~1-in-3 failure.

Hallucination, same card [verified]: **SimpleQA hallucination rate 0.782; PersonQA
0.491.** OpenAI's own explanation is that smaller models have less world knowledge
and that grounding/browsing reduces it. For this project that reframes to: *the
model must be forced to cite the note span for every fact it commits*, because
unanchored recall is wrong roughly half the time on person-shaped questions.

**BFCL v4** does not list gpt-oss-120b at all [verified,
[llm-stats BFCL-V4](https://llm-stats.com/benchmarks/bfcl-v4),
[Gorilla leaderboard](https://gorilla.cs.berkeley.edu/leaderboard.html)]. The
open-weight leaders there are the Qwen3.5 family (0.729 / 0.722 / 0.685 for
397B-A17B / 122B-A10B / 27B), against a 0.750 top score and a 0.583 all-model
average. So: **the best open-weight function-callers are Qwen, and gpt-oss-120b has
no independent multi-turn function-calling score at all.**

### 4.2 Tool calling with gpt-oss-120b is operationally fragile

This is the most under-appreciated risk and it is entirely verifiable from issue
trackers:

- vLLM `/v1/chat/completions` tool calling with gpt-oss-120b: `--tool-call-parser
  hermes` fails to start; omitting the parser errors; `mistral`/`llama3_json`
  parsers start but produce "incorrect number of parameters" or empty arguments.
  **It works on the `/v1/responses` (Harmony) endpoint** [verified,
  [vLLM #22578](https://github.com/vllm-project/vllm/issues/22578),
  [#22337](https://github.com/vllm-project/vllm/issues/22337)].
- Parallel tool calls: the model emits a tool call **and then hallucinated content
  after it** — it wasn't trained to stop after parallel calls. The upstream fix
  suppresses the trailing content **at the cost of disabling parallel tool calls
  entirely** [verified,
  [HF discussion #151](https://huggingface.co/openai/gpt-oss-120b/discussions/151)].
- Raw **Harmony channel markup leaks into agent output** in LangChain agent loops
  [verified,
  [LangChain forum](https://forum.langchain.com/t/harmony-response-format-sometimes-outputted-when-using-gpt-oss-120b-as-an-agent/2554)].
- Inference engines use the Jinja chat template rather than OpenAI's `harmony`
  library, and discrepancies between the two have been found [verified,
  [Unsloth run guide](https://unsloth.ai/docs/models/gpt-oss-how-to-run-and-fine-tune)].

**Design consequence: the tool-call surface must be treated as a lossy channel with
a validator in front of it, not as an API.**

### 4.3 Multi-turn is where open models fall apart

[verified, [arXiv:2505.06120, "LLMs Get Lost In Multi-Turn
Conversation"](https://arxiv.org/abs/2505.06120)] — 200,000+ simulated
conversations, every top open- and closed-weight model tested: **average 39%
performance drop** when a single-turn instruction is *sharded* across turns.
Decomposed: aptitude −15%, **unreliability +112%**. Root cause: models make
assumptions in early turns, prematurely commit to a solution, and then over-rely on
it.

This is the single most important result for the proposal, because **"each note
starts an agent conversation, and the owner's replies correct the graph" is
precisely the sharded-instruction setting the paper measures.** The degradation is
not a prompt-quality problem; it is the shape of the interaction. And it is worse
for smaller models.

### 4.4 The vision side

Qwen3-VL reports 80.56% on a hallucination benchmark (vs 85.0% Kimi-K2.6, 78.0%
GPT-5.5) and OCR across 32 languages [vendor,
[Qwen3-VL technical report](https://arxiv.org/pdf/2511.21631),
[Qwen blog](https://qwen.ai/blog?id=99f0335c4ad9ff6153e517418d48535ab6d8afef&from=research.latest-advancements-list)].
Independent handwriting evidence is less flattering: on a Japanese handwriting-OCR
benchmark a **ceiling around 0.80 persists even for the strongest models**
[verified, [JaWildText](https://arxiv.org/pdf/2603.27942)], and
[WildHandBench](https://arxiv.org/html/2608.22959) exists precisely because
handwritten text still defeats MLLMs. Photographed handwritten notes are therefore
a **~20%-error input**, and the pipeline must not treat an attachment's OCR as
equal-confidence evidence to typed body text.

### 4.5 The box

gpt-oss-120b is MXFP4 with ~5.1B active params/token; GGML-converted MXFP4 weights
occupy roughly **61 GB** and run at **~30 tok/s** on a 128 GB unified-memory Ryzen
AI Max+ 395 via LM Studio/ROCm llama.cpp [secondary but consistent,
[MindStudio benchmark](https://www.mindstudio.ai/blog/local-llm-benchmarks-ryzen-ai-halo),
[tenten](https://developer.tenten.co/the-most-economical-ways-to-run-gptoss120b)].

30 tok/s is the design constraint nobody costs properly. A Graphiti-style
six-LLM-call ingest, or an agent loop with 8–15 tool round-trips, at 30 tok/s, with
a vision model contending for the same memory, is **minutes per note**. The current
deterministic pipeline's two calls are not two calls by accident.

---

## 5. Constrained decoding / structured generation

### 5.1 Maturity: solved enough to depend on

**XGrammar is the default structured-generation backend for vLLM, SGLang and
TensorRT-LLM**; Outlines and lm-format-enforcer remain selectable in vLLM
[secondary but corroborated across sources,
[SqueezeBits guided-decoding benchmark](https://blog.squeezebits.com/guided-decoding-performance-vllm-sglang)].

Per-token overhead [verified,
[XGrammar-2, arXiv:2601.04426](https://arxiv.org/html/2601.04426)]:

| Engine | per-token overhead |
|---|---|
| XGrammar | <40 µs |
| llguidance (Harmony format) | ~250 µs |
| llguidance (Llama tool-call format) | >1000 µs |
| XGrammar-2 | ~13 µs; ≤6% gap to unconstrained end-to-end |

Robustness differs from speed: **llguidance is fastest with zero timeouts but has
more grammar-compilation failures; XGrammar handles more schemas but times out on
complex ones** [secondary, SqueezeBits]. Serving-level caveat: vLLM shows real
throughput degradation with guided decoding at batch ≥8, while SGLang overlaps mask
generation with GPU work.

### 5.2 Does it compose with tool calling? Now, yes.

This was the genuine gap and it closed. XGrammar-2 exists specifically for
**agentic** structured generation [verified, same paper]: a *cross-grammar cache*
so a different tool subset per request doesn't force recompilation
(inter-request dynamism), and **`TagDispatch`** — a grammar construct for
tag-triggered switching between free-form text and structured sub-grammars
(intra-request dynamism: the tool *name* selects the argument schema). 6× faster
compilation, 7× lower end-to-end latency than XGrammar.

`TagDispatch` is the mechanism that makes "free reasoning, then a
grammar-constrained tool call" a *supported configuration* rather than a hack —
which matters enormously given §5.3.

### 5.3 The accuracy debate, resolved

- **"Let Me Speak Freely?"** [verified,
  [arXiv:2408.02442](https://arxiv.org/pdf/2408.02442)] reported format
  restrictions degrading LLM performance, with both JSON and XML hurting weaker
  models — attributed to the general burden of format compliance rather than any
  one format.
- **The dottxt rebuttal** [verified,
  [Say What You Mean](https://blog.dottxt.ai/say-what-you-mean.html)] shows the
  paper conflated *JSON mode* with *structured generation*, and that its worst
  case (the "Last Letter" task) turned on using claude-3-haiku as an answer parser
  where standard harnesses use a regex. Their own runs have structured generation
  *outperforming* unstructured.
- The mechanism behind the real effect is field ordering: JSON-mode responses
  "consistently placed the `answer` key before the `reason` key, bypassing the
  chain-of-thought" [verified, same paper]. **Forced function calling collapses
  accuracy hardest because models emit only tool-call arguments with no
  deliberation surface at all.**
- **"Capacity, Not Format"** [verified,
  [arXiv:2606.09410](https://arxiv.org/pdf/2606.09410)] frames the residual as a
  *capacity tax*: format compliance consumes capacity, which bites hardest on
  smaller models. Mitigations: reduce format complexity, don't abandon structure.

**Net rule, and it is unambiguous: constrain the *output*, never the *thinking*,
and always put reasoning fields before answer fields.** For a 120B-class local
model — squarely in the "smaller model, pays the capacity tax" band — a schema with
fewer, flatter fields is not cosmetic.

---

## 6. Anti-patterns and postmortems

**A1 — The mega-prompt.** Graphiti v1: one prompt doing extraction + resolution +
context. Failed to scale with graph size and hallucinated more on smaller models
[vendor-but-mechanistic, Zep blog above]. JBrain2 has its own instance of this
already diagnosed: a **28 KB v28 extraction prompt opening with "CAPTURE EVERYTHING
THE NOTE STATES," ~8 MUST-emit rule blocks and a `min_facts: 12` floor**
(`ENTITY_GRAPH_REFOCUS_PLAN.md` §Thesis). A conversational agent whose system
prompt inherits that content is the same anti-pattern in a new wrapper.

**A2 — LLM-decided DELETE/UPDATE.** mem0 shipped it, then removed it for silent
information loss (§1.2).

**A3 — Ontology drift.** The failure is gradual: the ontology governing the graph
stops matching reality, LLM-extracted batches introduce format inconsistency and
semantic drift, and you end up with duplicates and inadequate entity resolution
[secondary,
[Graph Praxis, "Ontology Drift"](https://medium.com/graph-praxis/ontology-drift-why-your-knowledge-graph-is-slowly-going-wrong-234fa238826c)
— page returns 403 to automated fetch, so this rests on its indexed summary]. The
academic version: LLM extractors "lack sensitivity to entity classes," cannot reason
over existing relational paths, and produce conclusions contrary to fact on
equivalence/disjointness because they have no symbolic reasoning step and rely on
semantic correlation [verified,
[Ontology-Enhanced KG Completion](https://arxiv.org/html/2507.20643v2)].

**A4 — Semantic drift in never-ending learners.** NELL, the canonical
"machine reads and builds a KG forever" project: *once NELL learns a wrong entity
for a category, it drifts to learn other wrong examples because they share
patterns in web documents* [verified,
[Wikipedia/NELL](https://en.wikipedia.org/wiki/Never-Ending_Language_Learning),
[CACM "Never-Ending Learning"](https://dl.acm.org/doi/10.1145/3191513)]. The
published mitigation was **crowd-powered periodic human correction** — i.e. the
answer to autonomous graph maintenance was *never* "trust the loop"; it was
"schedule the human" [verified,
[Conversing Learning](https://www.researchgate.net/publication/294282523_Conversing_Learning_Active_Learning_and_Active_Social_Interaction_for_Human_Supervision_in_Never-Ending_Learning_Systems)].
This is a 15-year-old result and it is the closest historical analogue to the
proposed system.

**A5 — Extraction as replacement for text.** [verified,
[arXiv:2601.00821, "Verbatim Chunks Beat Extracted
Artifacts"](https://arxiv.org/html/2601.00821v3)] — a controlled ablation swapping
*only* the stored representation inside one fixed retrieve-rerank-reason pipeline.
**Verbatim chunks beat extracted artifacts by 15.9 points on LoCoMo (43.9% vs
28.0%) and 22.0 points on LongMemEval-S (67.4% vs 45.4%)**; the extracted-artifact
pipeline never beat naive RAG. Mechanism: *lossy distillation* — extraction
discards verbatim detail chunks retain for free. Their recommendation: a
`chunks ∪ artifacts` union store matches chunks on both benchmarks, so **structured
memory should augment verbatim text, never replace it.** (Caveat: LoCoMo's own
integrity problems from §1.11 apply to the absolute numbers; the *ablation* is
internally controlled and the direction is the finding.)

**A6 — Silent corruption is undetectable by construction.** Graphiti and mem0 give
the user no way to notice their memory holds contradictions [per TOKI, §3.3]. Letta's
self-editing memory fails silently when the model just doesn't save something. In
both cases the system has no notion of "I should have known this."

**A7 — Bundling memory maintenance into the conversational agent.** Letta's own
retreat (§1.3).

---

## 7. The 7 lessons that should change this design

**L1 — Do not let the agent both converse and write in one loop.**
Letta shipped exactly that (MemGPT), found reliability suffered because the agent
was juggling memory tools and task tools, and split memory maintenance into an
asynchronous sleep-time agent
([letta.com/blog/sleep-time-compute](https://www.letta.com/blog/sleep-time-compute/)).
Combine with the 39% multi-turn degradation and +112% unreliability from
[arXiv:2505.06120](https://arxiv.org/abs/2505.06120), and "one conversation per
note that both interviews the owner and mutates the graph" is the least reliable
available shape. *Change:* keep the conversation as an **intent-producing** surface
whose output is still validated and applied by the deterministic arbiter/apply
path. The agent proposes; the arbiter writes. This preserves the existing per-kind
conflict policy, the firewall floors, and the RLS-scoped write path — all of which
an agent with direct write tools bypasses.

**L2 — Never give the agent DELETE, and never ask it to diff against existing state
in the same call that reads the note.**
mem0 shipped ADD/UPDATE/DELETE reconciliation and *removed* it, citing overwrites
that "erased key information," deletes that removed later-relevant information, and
reconciliation as "where context got destroyed"
([mem0.ai](https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm)).
*Change:* the tool surface is `propose_fact` / `propose_supersession` /
`ask_owner`. There is no `delete_fact`. Retraction remains what it already is —
a consequence of deleting the source note.

**L3 — Cap the ask budget hard, spend it early, and batch it.**
"Ask or Assume?" recommends **≤3 interactions per task** and shows the forced-ask
baseline achieves comparable results with pure added friction
([arXiv:2603.26233](https://arxiv.org/abs/2603.26233)); SAGE-Agent gets *better*
coverage with **1.39 questions vs 2.56** using an EVPI-minus-cost stopping rule at
α≈0.1 ([arXiv:2511.08798](https://arxiv.org/abs/2511.08798)); "Ask Early, Ask Late"
shows goal-level ambiguity has a **≤10%-of-trajectory** window and that late asks
waste up to 21.7% of the work
([arXiv:2605.07937](https://arxiv.org/html/2605.07937v1)). *Change:* one
question-batch per note, at most ~2 questions, asked up front, or none. A note that
would generate a third question gets committed at low confidence and left for a
periodic sweep — never a trickle of follow-ups.

**L4 — The review queue's budget is a *rate*, and exceeding it destroys the queue.**
CDS override rates run **46.2%–96.2%**; a three-year study found **73.3% of alerts
overridden with 40% of those overrides inappropriate**; **88.2% of "very severe"
alerts were still overridden**
([systematic review](https://www.metrohealth.org/globalassets/metrohealth-documents/population-health-research-institute/ray-wilson-et-al-2026-alert-fatigue-systematic-review.pdf),
[JMIR](https://medinform.jmir.org/2022/10/e40511)). The refocus plan already
identified the mechanism locally (one card per raw predicate spelling, forever).
*Change:* make the queue's arrival rate an explicit, monitored budget — e.g. *N
items/week* — with the agent's confidence threshold **auto-tuned to hit it**, not a
fixed threshold that produces whatever volume it produces. A queue that exceeds
budget should suppress its own low-value items rather than grow.

**L5 — Structure the graph less, and keep the note text load-bearing.**
The controlled ablation in
[arXiv:2601.00821](https://arxiv.org/html/2601.00821v3) has verbatim chunks
beating extracted artifacts by **15.9 / 22.0 points**, with `chunks ∪ artifacts`
matching chunks — extraction is *lossy distillation*. GraphRAG's own trajectory
points the same way: extraction is ~75% of indexing cost, and the fix was
LazyGraphRAG's deferral to query time. *Change:* this is direct external validation
of the entity-graph refocus ("spine, not encyclopedia"), and an argument to go
further — an agentic rewrite should **reduce** the number of facts committed per
note, not increase it. Any design where the agent commits more than the current
pipeline is moving the wrong way.

**L6 — Name the four timestamps, and separate "was wrong" from "stopped being
true."**
Graphiti carries `t'_created`/`t'_expired`/`t_valid`/`t_invalid`
([arXiv:2501.13956](https://ar5iv.labs.arxiv.org/html/2501.13956)); SQL:2011,
XTDB and BiTRDF all model the two axes explicitly. TOKI's critique of Graphiti and
mem0 — no formal contradiction model, irreversible updates, transaction/valid time
conflated, silent belief corruption — applies to any system that encodes belief
time only implicitly ([arXiv:2606.06240](https://arxiv.org/pdf/2606.06240)).
*Change:* while the DB is disposable, promote transaction time to real columns
(`asserted_at` / `retracted_at`) alongside `valid_from`/`valid_to`, and make
**retract** (agent was wrong; close transaction time, valid time untouched) a
distinct operation from **supersede** (world changed; close valid time). An agent
that writes the graph makes "what did the system believe last Tuesday and why"
a debugging necessity, not a luxury.

**L7 — Treat the local tool-call channel as lossy, and constrain output without
constraining reasoning.**
gpt-oss-120b's τ-bench Airline tops out at **49.2%** and Retail at **67.8%** at high
reasoning ([model card](https://arxiv.org/pdf/2508.10925)); it is absent from BFCL
v4 entirely, where the open-weight leaders are Qwen3.5
([llm-stats](https://llm-stats.com/benchmarks/bfcl-v4)); and its tool calling is
demonstrably fragile in practice — broken chat-completions parsers in vLLM
([#22578](https://github.com/vllm-project/vllm/issues/22578)), hallucinated content
after parallel tool calls with the fix disabling parallel calls
([HF #151](https://huggingface.co/openai/gpt-oss-120b/discussions/151)), Harmony
markup leaking into agent output
([LangChain forum](https://forum.langchain.com/t/harmony-response-format-sometimes-outputted-when-using-gpt-oss-120b-as-an-agent/2554)).
*Change:* grammar-constrain every tool call (XGrammar/XGrammar-2 is now the default
backend across vLLM/SGLang/TensorRT-LLM, ~13–40 µs/token, and `TagDispatch`
explicitly supports free-text-then-structured switching —
[arXiv:2601.04426](https://arxiv.org/html/2601.04426)); put reasoning fields
*before* answer fields (the JSON-mode CoT-bypass effect,
[arXiv:2408.02442](https://arxiv.org/pdf/2408.02442)); keep schemas flat because
the format tax lands hardest on smaller models
([arXiv:2606.09410](https://arxiv.org/pdf/2606.09410)); assume **no parallel tool
calls**; and budget the loop against ~30 tok/s
([MindStudio 128 GB benchmark](https://www.mindstudio.ai/blog/local-llm-benchmarks-ryzen-ai-halo)).

---

## 8. Steal vs avoid

### Worth stealing

| Idea | From | Why |
|---|---|---|
| Six narrow prompts, not one mega-prompt; hybrid-search-capped dedup context | Graphiti ([Zep blog](https://blog.getzep.com/llm-rag-knowledge-graphs-faster-and-more-dynamic/)) | Prompt cost stops scaling with graph size; small models hallucinate less on narrow tasks |
| EVPI-minus-cost question selection with an explicit stop threshold (α≈0.1) | SAGE-Agent ([2511.08798](https://arxiv.org/abs/2511.08798)) | Principled, model-agnostic answer to "is this question worth asking"; validated on Qwen-14B |
| Hard interaction budget (≤3, ideally 1 batch) | Ask or Assume ([2603.26233](https://arxiv.org/abs/2603.26233)) | Over-asking costs friction without buying accuracy |
| Ask goal-level ambiguity *first* or never | Ask Early Ask Late ([2605.07937](https://arxiv.org/html/2605.07937v1)) | ≤10% window for goal info; late asks waste up to 21.7% of the work |
| Explicit 4-timestamp bitemporal columns; retract ≠ supersede | Graphiti / XTDB / TOKI | Point-in-time-of-belief replay; correction notes get a clean primitive |
| An `ontology_valid`-style tier flag carried on the instance and queryable | Cognee | "Show me everything the model coined on its own" becomes one query; extends the existing `declares_predicate` tier |
| Episodic node as a first-class retrievable object, semantic triples linked back | AriGraph ([2407.04363](https://arxiv.org/abs/2407.04363)) | Makes it safe for the graph to be deliberately lossy |
| `chunks ∪ artifacts`, never artifacts alone | [2601.00821](https://arxiv.org/html/2601.00821v3) | The extraction-only pipeline never beat naive RAG |
| Grammar-constrained tool calls with `TagDispatch`; reasoning-before-answer field order | XGrammar-2 ([2601.04426](https://arxiv.org/html/2601.04426)), [2408.02442](https://arxiv.org/pdf/2408.02442) | Structure without paying the CoT-bypass penalty |
| Async "sleep-time" maintenance agent, separate from the conversational one | Letta ([blog](https://www.letta.com/blog/sleep-time-compute/)) | Fits the existing nightly hygiene job; keeps the interactive path simple |
| Scheduled human correction as a *designed* component, not a fallback | NELL ([CACM](https://dl.acm.org/doi/10.1145/3191513)) | 15 years of evidence that unattended graph learning drifts |

### Worth avoiding

| Anti-pattern | Evidence |
|---|---|
| LLM-decided DELETE or in-place overwrite of stored facts | mem0 shipped and removed it; "reconciliation is where context got destroyed" ([mem0](https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm)) |
| One agent that both converses and writes | Letta's own retreat; 39% multi-turn drop / +112% unreliability ([2505.06120](https://arxiv.org/abs/2505.06120)) |
| Trusting an LLM's contradiction judgment as the sole arbiter | TOKI's failure taxonomy ([2606.06240](https://arxiv.org/pdf/2606.06240)); JBrain2's per-kind policy is strictly better |
| A completeness-maximizing extraction prompt ("capture everything", `min_facts` floor) | Graphiti's mega-prompt postmortem; A5 lossy-distillation result; GraphRAG cost cliff |
| Unbounded review-card generation (one card per novel surface form) | CDS override rates 46–96%; the refocus plan's own `new_predicate` diagnosis |
| Justifying the rewrite with LoCoMo / DMR / LongMemEval deltas | [zep-papers #5](https://github.com/getzep/zep-papers/issues/5); LoCoMo audit: 6.4% wrong answer key, judge accepts 63% of wrong answers |
| Strict-schema extraction that rejects out-of-vocabulary predicates | LlamaIndex's own comparison: most consistent, "might miss a lot of relationships" ([docs](https://developers.llamaindex.ai/python/examples/property_graph/dynamic_kg_extraction/)) |
| Relying on parallel tool calls, or on chat-completions tool parsers, with gpt-oss-120b | [HF #151](https://huggingface.co/openai/gpt-oss-120b/discussions/151), [vLLM #22578](https://github.com/vllm-project/vllm/issues/22578) |
| Treating attachment OCR as equal-confidence evidence to typed note body | Handwriting OCR ceiling ~0.80 ([JaWildText](https://arxiv.org/pdf/2603.27942), [WildHandBench](https://arxiv.org/html/2608.22959)) |
| A graph only the writing agent can interpret | Rewind's un-exportable archive ([notice](https://rewind.ai/what-happened-to-rewind/)) |

---

## Open questions for the owner

1. **Is the agent allowed to write, or only to propose?** L1 says propose-only,
   with the existing arbiter/apply path unchanged. That keeps per-kind conflict
   policy, domain firewalls and RLS on the write path — but it also means the
   "conversational agent that maintains the graph" is really "a conversational
   front-end to the existing pipeline." Is that still the change you want?

2. **What is your weekly tolerance for questions?** L3/L4 want a number, not a
   confidence threshold. Two questions a week? Ten? The number sets the agent's
   commit threshold, and the CDS evidence says an unbudgeted queue is a dead queue.

3. **Interrupt-at-capture, or batch-later?** "Ask Early" says goal-level ambiguity
   is worth almost nothing after the first 10% of a trajectory. Do you want to be
   interrupted at the moment of capture (when context is hot but you're busy), or
   is a once-daily batch acceptable — accepting that goal-level questions asked a
   day later are close to worthless?

4. **Retract vs supersede — do you want them separated?** (L6.) This is a schema
   change while the DB is disposable and cheap now, expensive later. It also
   changes what a correction note *means*: today it retracts by superseding; the
   proposal is that it close transaction time instead, leaving the old fact true
   about its interval only if the world actually changed.

5. **Should the agentic path commit *fewer* facts than v28 does?** L5 argues yes,
   and that any design committing more is moving the wrong way. Would you accept an
   explicit per-note fact *ceiling* (the inverse of today's `min_facts: 12` floor)
   as the acceptance criterion for the rewrite?

6. **What is the acceptable per-note wall-clock budget?** At ~30 tok/s, a
   Graphiti-shaped six-call ingest or a 10-round agent loop is minutes per note on
   your box, contending with the vision model. Is "a note is fully integrated within
   N minutes" a requirement, or is overnight fine?

7. **Given gpt-oss-120b's τ-bench Airline ~49% and its absence from BFCL v4, do you
   want to reconsider the model for the tool-driving role?** The open-weight
   function-calling leaders are the Qwen3.5 family; you already run a Qwen vision
   model. A Qwen text model for tool calling plus gpt-oss for reasoning is a real
   option, at the cost of a second resident model on a 128 GB box.

8. **How is a wrong committed fact ever noticed?** Every system in §1 fails silently
   here (A6). Correction notes only work if the owner *sees* the error. Is there
   appetite for a periodic "here are 5 facts I committed without asking, are any of
   these wrong?" sample — a spot-check budget distinct from the review queue?
