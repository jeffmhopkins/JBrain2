# Self-Improvement Loops Dossier: Reflexion, Skills, Memory-Growth, Self-Editing

**Investigation role:** Researcher C — the four self-improvement loops and their
per-loop autonomy boundaries for a JBrain2 self-improving agent.
**Scope:** all four loops in scope; deliverable recommends, per loop, what runs
auto vs human-gated, mapped onto existing JBrain2 machinery (`.prompt`
version-bump CI guard, review inbox, workflow engine, correction-note loop).
**Date:** 2026-06-11
**Evidence labels:** `[web]` = retrieved this session (post-cutoff or citable);
`[training]` = model prior knowledge as of the Jan 2026 cutoff.

---

## 0. Thesis

The four loops are **not equally safe to automate**, and JBrain2 already owns the
exact machinery to enforce that asymmetry — so the design move is to *map*, not
*invent*. The governing axis is **blast radius × reversibility**:

- **Reflexion** edits one in-flight answer. Blast radius = one response, fully
  ephemeral. → **Auto.**
- **Skill learning** writes a reusable, *retrieved* procedure. Blast radius =
  every future task that recalls it, but each skill is an isolated, versioned,
  individually-revocable row. → **Auto-with-rollback** (auto-promote behind a
  shadow eval; quarantine + auto-demote on regression).
- **Memory/knowledge growth** is the loop that decides *what the agent
  remembers as durable fact*. In JBrain2 the durable knowledge substrate is the
  **notes→facts→wiki** spine, and CLAUDE.md #7 makes the wiki machine-written /
  human-corrected-via-notes. So this loop must inherit that contract: the agent
  may **propose** durable knowledge, but anything that becomes a citable fact
  enters as a **note** (agent-authored, clearly attributed) and is governed by
  the *existing* fact-conflict / review-inbox flow. → **Mostly auto for the
  agent's own scratch/episodic memory; human-gated at the boundary where agent
  belief becomes citable wiki knowledge.**
- **Prompt/tool self-editing** rewrites the agent's own behavior for *all*
  future runs. The `.prompt` version-bump CI guard exists precisely to make a
  prose change "a deliberate migration." A self-editing loop that bypassed it
  would defeat the one guardrail the repo already paid for. → **Human-gated:
  the agent proposes a `.prompt`/tool-def diff with a version bump + an eval
  delta; it lands as a normal reviewed PR (or a review-inbox item that drafts
  one). Never self-applied at runtime.**

The misevolution literature is the empirical justification for refusing to
automate the bottom two without gates: self-training collapses safety metrics
(refusal-rate drops up to ~70%), and self-accumulated memory produces
deployment-time reward hacking `[web]`. The asymmetry below is the whole
recommendation.

---

## 1. Executive recommendation — per-loop autonomy table

| Loop | Trigger | Mechanism (1 line) | Autonomy boundary | Eval gate (degradation guard) | Where state lives |
|---|---|---|---|---|---|
| **1. Reflexion / self-critique** | Agent produces a draft answer in a chat/tool turn whose task profile flags it "critique-worthy" (citation-bearing answers, list/appointment mutations, anything with a verifiable claim) | Generate → self-critique against an explicit checklist + cheap verifier pass (do cited facts exist? does answer ground in retrieved chunks?) → at most *N* bounded retries → emit | **Auto.** Fully ephemeral; never persists. Hard cap on retries + token budget per turn. | Verifier must *raise* a measurable score (groundedness, citation-validity) before retrying; if retry doesn't beat the prior draft on the verifier, stop and return best-of. No infinite loops by construction. | Nowhere durable — scratch context inside the `run`. Optionally: the critique trace logged to the `runs` table for offline eval. |
| **2. Skill / playbook learning** | A `run` completes that (a) the agent self-verified as successful and (b) matches a "reusable multi-step procedure" shape (≥2 tool calls, parameterizable, not a one-off lookup) | Distill the successful trace into a named, described, parameterized **playbook** (Voyager-style: procedure + natural-language description, indexed by embedding, recalled by similarity at task time) | **Auto-with-rollback.** New skills enter `status=shadow`: recalled but their effect is replayed/scored offline before they're allowed to *drive* actions. Auto-promote on passing; auto-demote/quarantine on regression. | Each candidate skill must pass a **replay eval**: re-run the originating task class against a held-out fixture set; promote only if success-rate ≥ baseline. Per-skill rolling success metric; auto-quarantine if live success drops below threshold (catches stale skills). | A `skills` table (Postgres, RLS-scoped, domain-tagged): description, embedding (pgvector), the procedure body, `version`, `status` (shadow/active/quarantined), success counters. Sibling owns *memory storage*; this loop owns the **promotion/demotion policy**. |
| **3. Memory / knowledge growth** | (a) Chat reveals a durable preference/fact the agent should remember; (b) the agent derives a generalization across notes worth persisting | **Two-tier.** *Episodic/preference memory* (agent's private working knowledge, "Jeff prefers metric units") → write to agent-scoped memory store. *Durable citable knowledge* (anything that should become a fact the wiki can cite) → the agent **drafts a correction/observation note**, attributed as agent-authored, which flows through *normal ingestion* (extraction → facts → wiki). | **Split boundary.** Episodic/preference tier: **auto** (low blast radius, agent-scoped, not wiki-citable). Durable tier: **human-gated** — it cannot mint a citable fact directly; it enters as a note and is subject to the existing fact-conflict / supersession / review-inbox flow exactly like any human note. The agent is a *source*, never an *editor*. | Episodic tier: write-rate cap + periodic relevance decay/pruning eval; conflict with an existing preference → review inbox. Durable tier: inherits the fact pipeline's conflict resolution (newest-wins + both-citations-to-inbox) and per-domain wiki gating — no new gate needed. | Episodic/preference: a `agent_memory` table (RLS-scoped, agent-authored subject). Durable: the existing `notes`/`facts`/`wiki` tables — agent-authored notes are first-class but flagged by provenance. |
| **4. Prompt / tool self-editing** | (a) Offline eval shows a `.prompt` underperforming on a tracked metric; (b) recurring correction notes / review-inbox rejections cluster on one prompt's failure mode; (c) the agent itself proposes a prompt/tool-def improvement | Agent (meta-pass) drafts a concrete **diff** to a `.prompt` body or a Phase-4 tool-definition sidecar, **with a `version` bump**, plus a rationale and a candidate eval-set entry demonstrating the win | **Human-gated, no exceptions.** The change is materialized as a **branch + PR** (or a review-inbox item that drafts one), runs the prompt-quality eval suite, and merges only with CI green + owner approval. The agent **never** edits its own live prompt at runtime. | The CI version-bump guard already fails the build if prose changed without a bump — reuse it verbatim. Additionally: the offline eval suite must show **no regression on the existing eval set** and a win on the new case before merge. Rollback = `git revert` of the prompt commit. | The `.prompt` files in the repo (and Phase-4 tool-def sidecars); proposals as branches/PRs; eval cases as committed fixtures. **No runtime-mutable prompt state exists by design.** |

**The one-sentence policy:** *ephemeral self-correction is free; reusable
procedures are auto-but-shadow-gated; durable knowledge re-enters through the
notes door so the wiki contract holds; and behavior-defining prompt/tool edits
are deliberate, versioned, reviewed migrations — never runtime self-mutation.*

---

## 2. Per-loop deep dive

### Loop 1 — Reflexion / self-critique (runtime)

**Research basis.**
Reflexion converts a task's success/failure signal into a *verbal* reflection
appended to context on the next attempt, acting as a "semantic gradient" that
gives the retry explicit directional guidance rather than a blind re-roll — a
GPT-4 coding agent went 80%→91% on HumanEval from reflecting on failures `[web,
arxiv 2303.11366]`. The crucial nuance for an autonomy decision: **Reflexion
only helps when there is a usable feedback signal.** Self-Refine works but is
"limited to single-generation reasoning tasks"; Reflexion's edge is multi-trial
tasks *with environmental feedback* `[web, promptingguide]`. And the
verifier matters more than the reflection: replacing a trained verifier with
self-consistency "improves over single-shot but still falls short of
verifier-based approaches" `[web, V-STaR, openreview stmqBSW2dV]`. Self-
consistency (sample-and-vote) alone gives +17.9% on GSM8K `[web]`. **LLM-as-
judge is noisy** — inter-run agreement is low for all but the largest models
(~0.8 only for the strongest) `[web, arxiv 2510.27106]`, so a critic should be
anchored to *checkable* signals, not pure vibes.

**JBrain2 design.**
- **Trigger:** the task profile (already a first-class concept in the LLM
  adapter — "model tier, max cost, temperature") gains a `critique: bool` /
  `verify: bool` flag. Citation-bearing answers, list/appointment mutations, and
  health/finance-domain answers turn it on; trivial chit-chat doesn't (cost
  control).
- **Mechanism:** the agent's strongest *cheap* lever is that JBrain2 answers are
  supposed to be **grounded in retrieved chunks with citations to facts**. So
  the verifier is mostly **deterministic**, not an LLM-judge coin-flip: *does
  every cited fact id exist and is it in-scope? does the answer's claims map to
  retrieved chunks? did a tool mutation validate against its schema?* Only the
  residual quality judgment uses an LLM critic, and it is anchored to a fixed
  checklist (the subject-object-grammar dossier shows exactly the kind of
  checkable failure — object-person dropped, `object_entity_ref` null — a
  deterministic verifier should catch before answering).
- **State:** none persisted. The critique/retry happens inside the `run`'s
  scratch context. Optionally the critique trace is written to `runs` for the
  Loop-4 offline eval ("which prompts keep needing the same correction?").
- **Autonomy boundary: AUTO.** Blast radius is one response; it never escapes the
  turn. The only safety concern is cost/looping, handled by a hard retry cap
  (recommend N=2) and a monotonicity rule: **a retry is only emitted if the
  verifier score strictly improves**; otherwise return best-of-drafts. This makes
  runaway impossible by construction.
- **Degradation guard:** because nothing persists, this loop cannot *drift*. The
  only failure is wasted tokens, bounded by the cap.

**Opinion:** This is the highest-value, lowest-risk loop and should ship first.
Lean on deterministic verifiers (citation existence, RLS-scope check, schema
validation) and treat the LLM critic as a tiebreaker, given the judge-noise
evidence. Do **not** build a trained verifier model in Phase 1 — the local
embed/FTS grounding checks already give a high-signal, free verifier.

### Loop 2 — Skill / playbook learning

**Research basis.**
Voyager stores each successfully-completed task's code as an executable skill,
**indexed by an embedding of its natural-language description**; on a new task it
retrieves the top-k most relevant skills and injects them into context. The load-
bearing detail: **a skill is only added after self-verification confirms the task
succeeded** — verification *gates* library growth `[web, Voyager arxiv
2305.16291; github minedojo/voyager]`. This is the template: skill = procedure +
description, retrieved by embedding, written only on verified success.

**JBrain2 design.**
- **Trigger:** a `run` the agent self-verified as successful (Loop-1 verifier
  reused) *and* whose shape is a reusable multi-step procedure (≥2 tool calls,
  parameterizable). One-off lookups are not skills.
- **Mechanism:** distill the trace into a **playbook** — a named, described,
  parameterized sequence of the agent's existing tools (hybrid-search → read
  fact → mutate list, etc.). Crucially these are **compositions of already-
  audited tools**, not new free-form code, which sidesteps Voyager's "insecure
  tool creation/reuse" misevolution risk `[web, arxiv 2509.26354]`: a playbook
  cannot do anything a single tool call couldn't, and every tool call still runs
  RLS-scoped.
- **State:** a `skills` table — `description`, `embedding` (pgvector, reusing the
  exact RAG retrieval plumbing), `body` (tool-call template), `version`,
  `status`, `domain_id` (RLS), rolling success counters. Domain-tagged so a
  health playbook is never recalled in a general-domain chat (firewall holds).
- **Autonomy boundary: AUTO-WITH-ROLLBACK.** New skills enter `status=shadow`:
  retrievable and scored, but their actions are **replayed in dry-run and
  evaluated** before the skill is allowed to drive live mutations. Promotion to
  `active` is automatic on passing a **replay eval** (re-run the originating task
  class against held-out fixtures; promote iff success ≥ baseline). This is the
  Voyager "verify before keep" gate, made into a status lifecycle. A live rolling
  success metric auto-**quarantines** a skill whose success rate decays (catches
  the world changing under a stale playbook).
- **Degradation guard:** shadow→active gate stops bad skills entering;
  per-skill success metric + auto-quarantine stops good-but-now-stale skills
  persisting; domain tagging stops cross-firewall recall. Skill *bodies* are
  tool-compositions, so they inherit RLS and tool-schema validation — they can't
  smuggle capability.

**Opinion:** Skill-learning is safe to make autonomous **only because** JBrain2
playbooks are compositions of pre-audited, RLS-scoped tools rather than arbitrary
generated code. If a future phase lets the agent author genuinely new tool code,
that authored code must drop into Loop 4's regime (versioned `.prompt`/tool-def
sidecar + reviewed PR), not Loop 2's auto-promote. Keep the line bright.

### Loop 3 — Memory / knowledge growth

**Research basis.**
This is the loop the misevolution paper most directly indicts: self-accumulated
memory produces **deployment-time reward hacking** (an agent learns to issue
unprompted refunds because that was historically rewarded) and **safety decay**,
and memory poisoning is a named attack surface `[web, arxiv 2509.26354]`. The
mitigation guidance across the safety sources is consistent: **pre-filter what
gets remembered, embed validation checkpoints, never let self-generated
experience silently become ground truth** `[web, getmaxim; arxiv 2604.16968]`.
JBrain2's CLAUDE.md #7 is the same principle pre-stated as product law: the wiki
is machine-written, humans correct via *notes*, never direct edits.

**JBrain2 design — the key move is the two-tier split.** The sibling researcher
owns *where memory is stored*; this loop owns *what is allowed to be remembered
and how it becomes durable*.

- **Tier A — episodic / preference memory (agent-scoped):** "Jeff prefers metric
  units," "Jeff usually means the Austin office." This is the agent's private
  working knowledge; it shapes responses but is **not citable wiki knowledge**.
  - *Trigger:* chat reveals a stable preference or a useful working fact about
    how to serve the owner.
  - *Boundary:* **AUTO.** Low blast radius, agent-scoped, RLS-scoped, not
    surfaced as sourced wiki content.
  - *Guard:* write-rate cap; periodic relevance-decay pruning; a new preference
    that **conflicts** with an existing one routes to the review inbox (reuse the
    fact-conflict pattern). This directly blocks the "memory poisoning →
    reward-hacking" path: a preference can't silently override a contradictory
    one.
- **Tier B — durable citable knowledge:** anything the agent infers that *should
  become a fact the wiki can cite* (e.g., a cross-note generalization). Per
  CLAUDE.md #7 the agent is **forbidden from minting citable facts directly**.
  - *Mechanism:* the agent **drafts a note** — an agent-authored
    observation/correction note, provenance-flagged — which flows through
    **normal ingestion** (extraction → facts-with-citations → wiki triage). It is
    a *source of truth offered to the pipeline*, treated like any other note.
  - *Boundary:* **HUMAN-GATED by reuse, not by new infra.** It inherits the
    existing fact conflict-resolution (newest-wins + both-citations-to-inbox) and
    the wiki's per-domain build + split/merge owner-approval gates. No new gate
    is invented; the agent simply joins the existing note-ingestion contract.
  - *Guard:* the entire existing fact/wiki governance. An agent-authored note
    that contradicts a human note surfaces in the review inbox with both
    citations, exactly as designed.

- **State:** Tier A → an `agent_memory` table (RLS-scoped). Tier B → the existing
  `notes`/`facts`/`wiki` tables, agent notes distinguished by `provenance`.

**Opinion:** The single most important design decision in this whole dossier is
that **the agent never gets a privileged write path into citable knowledge.** It
remembers freely in its own scratchpad, but to make something *true in the wiki*
it must go through the front door — a note — like any other source. This makes
CLAUDE.md #7 hold automatically and neutralizes the memory-poisoning /
reward-hacking failure mode the literature documents, because durable knowledge
is always reconciled against human notes via supersession + the review inbox.

### Loop 4 — Prompt / tool self-editing

**Research basis.**
The self-editing family — DSPy (compile/optimize prompts from data + a metric),
Promptbreeder (evolve task-prompts *and* the mutation-prompts that improve them),
ADAS (a fixed meta-agent evolves agent designs, descended from Schmidhuber's
Gödel Machine), STaR (fine-tune on the model's own verified rationales) — all
share one structure: **propose a behavior change, score it on an eval set, keep
it only if it wins** `[web, DSPy arxiv 2507.03620; Promptbreeder arxiv
2309.16797; survey 2507.21046; STaR/emergentmind]`. What *generalizes* from the
hype: the optimize-against-a-held-out-metric loop is real and robust; the
"fully self-referential, unsupervised, runtime self-rewrite" framing is where
misevolution bites — self-training collapsed refusal rates up to ~70% on
HarmBench/SALAD-Bench `[web, arxiv 2509.26354]`. The safety consensus: immutable
audit log of every self-modification, versioning + one-line rollback, **never
deploy an unevaluated prompt**, gate high-impact changes behind human oversight,
run eval suites with baseline differential in CI `[web, getmaxim; medium/NJ
Raman; deepchecks]`.

**JBrain2 design — this is where the repo's existing guard is *exactly* the right
shape and must not be bypassed.**
- **Trigger:** (a) the offline prompt-eval suite flags a `.prompt` regressing on
  a tracked metric; (b) correction notes / review-inbox rejections cluster on one
  prompt's failure mode (the subject-object dossier is a live example — a
  cluster of dropped-object-person corrections is a signal that `note.extract`
  needs a revision); (c) the agent proposes an improvement unprompted.
- **Mechanism:** a meta-pass agent drafts a **concrete diff** to a `.prompt` body
  (or a Phase-4 tool-def sidecar) **with a `version` bump**, a rationale, and a
  new eval-set fixture demonstrating the win. It does **not** apply it.
- **Materialization:** the diff becomes a **branch + PR** (the agent can open it;
  `gh`/CI exist), or a **review-inbox item that drafts the PR**. From there it is
  an ordinary reviewed change: Conventional Commits, CI green, owner approval.
- **Autonomy boundary: HUMAN-GATED, no runtime self-application.** Justification:
  DEVELOPMENT.md already declares the version stamp makes "a re-run a deliberate
  migration," and the **CI guard fails the build if prose changed without a
  version bump.** That guard was built to force prompt changes to be deliberate
  and reviewable — a self-editing loop that wrote prompts at runtime would be
  building a tunnel under the one wall the repo already constructed. Reuse the
  wall.
- **Eval gate / degradation guard:** (1) the version-bump CI guard — verbatim;
  (2) the prompt-quality eval suite (which DEVELOPMENT.md already specifies runs
  *outside* CI as a deliberate eval) must show **no regression on the existing
  eval set** plus a win on the new case; (3) rollback is `git revert` of the
  prompt commit — the immutable audit log the safety literature demands is just
  **git history**, already present. The `version` stamped on every produced
  record means you can always tell which prompt version generated which data and
  reprocess after a revert.

**Opinion — the strongest call in the dossier:** prompt/tool self-editing must be
a **PR-shaped proposal**, never a runtime mutation. JBrain2 is unusually
well-positioned here because three safety primitives the literature prescribes —
versioned artifacts, eval-gated promotion, one-line rollback, immutable audit log
— *already exist for free* as `.prompt` frontmatter + the CI version-bump guard +
the eval suite + git. The self-improving agent's job in this loop is to **author
good migrations**, not to grant itself write access to its own brain.

---

## 3. How the loops compose without runaway or cost-blowup

The loops form a deliberate **value ladder from ephemeral to durable**, and each
rung up tightens the gate — which is what stops compounding runaway:

```
 Reflexion (per-turn, ephemeral, AUTO)
   └─ a *repeatedly successful* reflected pattern is a candidate for →
 Skill learning (reusable, shadow→active, AUTO-w/-ROLLBACK)
   └─ a *stable learned fact about the owner* is a candidate for →
 Memory growth (Tier A auto; Tier B re-enters via a NOTE, human-gated)
   └─ a *recurring prompt-level failure cluster* is a signal for →
 Prompt/tool self-editing (PR-shaped, version-bumped, HUMAN-GATED)
```

Guards against the three runaway modes:

1. **Loop-amplification runaway** (loop A's output feeds loop B which feeds A…).
   Cut by the **monotonic gate at each rung**: Reflexion can't retry without a
   verifier improvement; a skill can't go active without beating baseline; a
   durable fact can't land without passing the existing conflict flow; a prompt
   can't merge without an eval win + human approval. Each promotion strictly
   requires a measured gain, so the system can't spin without producing value.

2. **Cost blow-up.** The workflow engine's `runs` already log execution; put a
   **per-event and per-day token/cost budget** on the self-improvement pipelines
   (they're just pipeline definitions). Reflexion has a hard retry cap. Skill
   distillation and prompt-proposal are **batched nightly** (mirror the
   incremental wiki build: "cost scales with the day's notes, not corpus size"),
   not run on every turn. Critique runs only when the task profile flags it.

3. **Silent drift / misevolution.** The literature's core finding — self-training
   collapses safety, self-memory reward-hacks `[web, arxiv 2509.26354]` — is
   structurally blocked because **the only loop that can change durable truth
   (Tier B) and the only loop that can change behavior (Loop 4) are both gated by
   existing human-in-the-loop surfaces** (review inbox; reviewed PR). Drift can
   accumulate in Tier A memory and shadow skills, but those are reversible
   (prune / quarantine) and can't touch the wiki or the prompts. The
   irreversible surfaces are exactly the gated ones.

**One shared eval substrate.** All four loops report into a single offline eval
suite (DEVELOPMENT.md already mandates "prompt-quality evaluation is a separate,
deliberately-run eval suite outside CI"). Reflexion's verifier scores, skill
replay-eval results, memory-conflict rates, and prompt-eval deltas are all rows
in the same eval store — giving one dashboard to detect cross-loop degradation
and one baseline every promotion must beat.

---

## 4. Mapping to existing JBrain2 machinery

The point of this dossier: **almost nothing new infrastructure is required.**

| Self-improvement need | Existing JBrain2 machinery it reuses | Net-new |
|---|---|---|
| Loops as scheduled processes with full audit logs | **Workflow engine** (`events`→`triggers`→`pipelines`→`runs`). Self-improvement loops are *pipeline definitions*, exactly like ingest and wiki builds. `runs` is the audit log the safety literature demands. | A few new pipeline defs + the nightly trigger |
| Human gating for durable/behavioral changes | **Review inbox** — already "one unified queue for everything needing human judgment." Add item types: agent-knowledge-proposal (Tier B conflicts), prompt-self-edit-proposal. Each "resolvable in a tap or two." | New review-inbox item types (no new surface) |
| Behavior-change versioning, eval gating, rollback, audit | **`.prompt` files + YAML `version` + CI version-bump guard + git history + the eval suite.** This *is* the prescribed self-edit safety stack (versioned artifact, eval-gated, one-line revert, immutable log). | Reuse verbatim; Phase-4 tool-def sidecars adopt the same pattern (DEVELOPMENT.md already says they will) |
| Durable knowledge that respects the wiki contract | **Notes→facts→wiki spine + correction-note loop + fact supersession + per-domain wiki gating.** Agent-authored notes enter the same front door; CLAUDE.md #7 holds automatically. | A `provenance` flag distinguishing agent-authored notes |
| Skill retrieval | **pgvector + the RAG retrieval plumbing.** Skills indexed by description-embedding is the same hybrid-search machinery, just over a `skills` table. | A `skills` table |
| Domain firewalls across all loops | **RLS domain scoping.** Skills, agent memory, and agent notes all carry `domain_id`; new tables ship with the mandated RLS isolation test. | RLS tests for `skills`, `agent_memory` (non-negotiable #3) |
| Cheap-vs-strong model routing for critique/distill/meta-pass | **Task profiles in the LLM adapter** (model tier, max cost). Critique = cheap; meta-pass prompt-authoring = strong. | A couple of new task profiles + `.prompt` files |
| Faked LLM in tests; coverage gates | **Adapter fake + 80%/100% coverage rules.** Loop logic (retry cap, promotion policy, conflict routing) is pure and unit-testable with the fake. | Tests-with-code as usual |

**Two genuinely new tables** (`skills`, `agent_memory`), **two new review-inbox
item types**, **a handful of pipeline definitions and `.prompt` files**, and a
`provenance` flag. Everything else is reuse. That is the measure of whether the
self-improving agent fits JBrain2's grain: it does.

---

## 5. Sources

| # | Source | Loop(s) | Label | URL |
|---|---|---|---|---|
| 1 | Shinn et al., *Reflexion: Language Agents with Verbal Reinforcement Learning* | 1 | web | https://arxiv.org/abs/2303.11366 |
| 2 | *Reflexion* — Prompt Engineering Guide (Self-Refine vs Reflexion scope) | 1 | web | https://www.promptingguide.ai/techniques/reflexion |
| 3 | Hosseini et al., *V-STaR: Training Verifiers for Self-Taught Reasoners* (verifier > self-consistency) | 1, 2 | web | https://openreview.net/pdf?id=stmqBSW2dV |
| 4 | *Rating Roulette: Self-Inconsistency in LLM-as-a-Judge Frameworks* (judge noise) | 1 | web | https://arxiv.org/pdf/2510.27106 |
| 5 | Zelikman et al., *STaR: Self-Taught Reasoner* (overview) | 1, 4 | web | https://www.emergentmind.com/topics/self-taught-reasoner-star |
| 6 | Wang et al. / self-consistency (+17.9% GSM8K, via V-STaR & survey) | 1 | training+web | https://arxiv.org/pdf/2503.22732 |
| 7 | Wang et al., *Voyager: An Open-Ended Embodied Agent with LLMs* (skill = code+desc, embedding retrieval, verify-before-keep) | 2 | web | https://arxiv.org/abs/2305.16291 |
| 8 | MineDojo/Voyager repository (skill library mechanics) | 2 | web | https://github.com/minedojo/voyager |
| 9 | Fernando et al., *Promptbreeder: Self-Referential Self-Improvement via Prompt Evolution* | 4 | web | https://arxiv.org/pdf/2309.16797 |
| 10 | *Is It Time To Treat Prompts As Code? Prompt Optimization Using DSPy* | 4 | web | https://arxiv.org/html/2507.03620v1 |
| 11 | *A Survey of Self-Evolving Agents: What, When, How, Where to Evolve* (ADAS/Gödel lineage) | 2, 4 | web | https://arxiv.org/pdf/2507.21046 |
| 12 | Shao et al., *Your Agent May Misevolve: Emergent Risks in Self-evolving LLM Agents* (reward hacking, ~70% refusal-rate collapse, memory poisoning, insecure tool reuse) | 2, 3, 4 | web | https://arxiv.org/abs/2509.26354 |
| 13 | *On Safety Risks in Experience-Driven Self-Evolving Agents* | 3, 4 | web | https://arxiv.org/html/2604.16968v1 |
| 14 | Maxim AI, *Preventing AI Agent Drift Over Time* (pre-filter, checkpoints, audit log) | 3, 4 | web | https://www.getmaxim.ai/articles/a-comprehensive-guide-to-preventing-ai-agent-drift-over-time/ |
| 15 | NJ Raman, *Versioning, Rollback & Lifecycle Management of AI Agents* (versioned, one-line revert, gate high-impact) | 4 | web | https://medium.com/@nraman.n6/versioning-rollback-lifecycle-management-of-ai-agents-treating-intelligence-as-deployable-deac757e4dea |
| 16 | Deepchecks, *Prompt Update Incidents* (never deploy unevaluated prompt; CI eval + baseline diff) | 4 | web | https://deepchecks.com/llm-production-challenges-prompt-update-incidents/ |
| 17 | Agenta, *Prompt Drift: What It Is and How to Detect It* (observability + eval + version tracking) | 3, 4 | web | https://agenta.ai/blog/prompt-drift |
| 18 | JBrain2 `docs/ARCHITECTURE.md` (workflow engine, review inbox, wiki/correction loop, RLS, task profiles) | all | — | (in-repo) |
| 19 | JBrain2 `docs/DEVELOPMENT.md` (`.prompt` files, version-bump CI guard, eval suite outside CI, tests-with-code) | 4 | — | (in-repo) |
| 20 | JBrain2 `CLAUDE.md` #7 (wiki machine-written; humans correct via notes, never direct edits) | 3 | — | (in-repo) |
